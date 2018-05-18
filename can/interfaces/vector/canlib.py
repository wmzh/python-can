#!/usr/bin/env python
# coding: utf-8

"""
Ctypes wrapper module for Vector CAN Interface on win32/win64 systems.

Authors: Julien Grave <grave.jul@gmail.com>, Christian Sandberg
"""

# Import Standard Python Modules
# ==============================
import ctypes
import logging
import sys
import time

try:
    # Try builtin Python 3 Windows API
    from _winapi import WaitForSingleObject, INFINITE
    HAS_EVENTS = True
except ImportError:
    try:
        # Try pywin32 package
        from win32event import WaitForSingleObject, INFINITE
        HAS_EVENTS = True
    except ImportError:
        # Use polling instead
        HAS_EVENTS = False

# Import Modules
# ==============
from can import BusABC, Message
from .exceptions import VectorError

# Define Module Logger
# ====================
LOG = logging.getLogger(__name__)

# Import safely Vector API module for Travis tests
vxlapi = None
try:
    from . import vxlapi
except Exception as exc:
    LOG.warning('Could not import vxlapi: %s', exc)


class VectorBus(BusABC):
    """The CAN Bus implemented for the Vector interface."""

    def __init__(self, channel, can_filters=None, poll_interval=0.01,
                 receive_own_messages=False,
                 bitrate=None, rx_queue_size=256, app_name="CANalyzer", fd=False, data_bitrate=None, **config):
        """
        :param list channel:
            The channel indexes to create this bus with.
            Can also be a single integer or a comma separated string.
        :param float poll_interval:
            Poll interval in seconds.
        :param int bitrate:
            Bitrate in bits/s.
        :param int rx_queue_size:
            Number of messages in receive queue.
        :param str app_name:
            Name of application in Hardware Config.
        :param bool fd:
            If CAN-FD frames should be supported.
        :param int data_bitrate:
            Which bitrate to use for data phase in CAN FD.
            Defaults to arbitration bitrate.
        """
        if vxlapi is None:
            raise ImportError("The Vector API has not been loaded")
        self.poll_interval = poll_interval
        if isinstance(channel, (list, tuple)):
            self.channels = channel
        elif isinstance(channel, int):
            self.channels = [channel]
        else:
            # Assume comma separated string of channels
            self.channels = [int(ch.strip()) for ch in channel.split(',')]
        self._app_name = app_name.encode()
        self.channel_info = 'Application %s: %s' % (
            app_name, ', '.join('CAN %d' % (ch + 1) for ch in self.channels))

        vxlapi.xlOpenDriver()
        self.port_handle = vxlapi.XLportHandle(vxlapi.XL_INVALID_PORTHANDLE)
        self.mask = 0
        self.fd = fd
        # Get channels masks
        for channel in self.channels:
            hw_type = ctypes.c_uint(0)
            hw_index = ctypes.c_uint(0)
            hw_channel = ctypes.c_uint(0)
            vxlapi.xlGetApplConfig(self._app_name, channel, hw_type, hw_index,
                                   hw_channel, vxlapi.XL_BUS_TYPE_CAN)
            LOG.debug('Channel index %d found', channel)
            mask = vxlapi.xlGetChannelMask(hw_type.value, hw_index.value,
                                           hw_channel.value)
            LOG.debug('Channel %d, Type: %d, Mask: %d',
                      hw_channel.value, hw_type.value, mask)
            self.mask |= mask

        permission_mask = vxlapi.XLaccess()
        # Set mask to request channel init permission if needed
        if bitrate or fd:
            permission_mask.value = self.mask
        if fd:
            vxlapi.xlOpenPort(self.port_handle, self._app_name, self.mask,
                              permission_mask, 16000,
                              vxlapi.XL_INTERFACE_VERSION_V4, vxlapi.XL_BUS_TYPE_CAN)
        else:
            vxlapi.xlOpenPort(self.port_handle, self._app_name, self.mask,
                              permission_mask, rx_queue_size,
                              vxlapi.XL_INTERFACE_VERSION, vxlapi.XL_BUS_TYPE_CAN)
        LOG.debug(
            'Open Port: PortHandle: %d, PermissionMask: 0x%X',
            self.port_handle.value, permission_mask.value)

        if permission_mask.value == self.mask:
            if fd:
                self.canFdConf = vxlapi.XLcanFdConf()
                if bitrate:
                    self.canFdConf.arbitrationBitRate = ctypes.c_uint(bitrate)
                else:
                    self.canFdConf.arbitrationBitRate = ctypes.c_uint(500000)
                self.canFdConf.sjwAbr = ctypes.c_uint(2)
                self.canFdConf.tseg1Abr = ctypes.c_uint(6)
                self.canFdConf.tseg2Abr = ctypes.c_uint(3)
                if data_bitrate:
                    self.canFdConf.dataBitRate = ctypes.c_uint(data_bitrate)
                else:
                    self.canFdConf.dataBitRate = self.canFdConf.arbitrationBitRate
                self.canFdConf.sjwDbr = ctypes.c_uint(2)
                self.canFdConf.tseg1Dbr = ctypes.c_uint(6)
                self.canFdConf.tseg2Dbr = ctypes.c_uint(3)
                
                vxlapi.xlCanFdSetConfiguration(self.port_handle, self.mask, self.canFdConf)
                LOG.info('SetFdConfig.: ABaudr.=%u, DBaudr.=%u', self.canFdConf.arbitrationBitRate, self.canFdConf.dataBitRate)
            else:
                if bitrate:
                    vxlapi.xlCanSetChannelBitrate(self.port_handle, permission_mask, bitrate)
                    LOG.info('SetChannelBitrate: baudr.=%u',bitrate)
        else:
            LOG.info('No init access!')

        # Enable/disable TX receipts
        tx_receipts = 1 if receive_own_messages else 0
        vxlapi.xlCanSetChannelMode(self.port_handle, self.mask, tx_receipts, 0)

        if HAS_EVENTS:
            self.event_handle = vxlapi.XLhandle()
            vxlapi.xlSetNotification(self.port_handle, self.event_handle, 1)
        else:
            LOG.info('Install pywin32 to avoid polling')

        self.set_filters(can_filters)

        try:
            vxlapi.xlActivateChannel(self.port_handle, self.mask,
                                     vxlapi.XL_BUS_TYPE_CAN, 0)
        except VectorError:
            self.shutdown()
            raise

        # Calculate time offset for absolute timestamps
        offset = vxlapi.XLuint64()
        vxlapi.xlGetSyncTime(self.port_handle, offset)
        self._time_offset = time.time() - offset.value * 1e-9

        super(VectorBus, self).__init__()

    def set_filters(self, can_filters=None):
        if can_filters:
            # Only one filter per ID type allowed
            if len(can_filters) == 1 or (
                    len(can_filters) == 2 and
                    can_filters[0].get("extended") != can_filters[1].get("extended")):
                for can_filter in can_filters:
                    try:
                        vxlapi.xlCanSetChannelAcceptance(
                            self.port_handle, self.mask,
                            can_filter["can_id"], can_filter["can_mask"],
                            vxlapi.XL_CAN_EXT if can_filter.get("extended") else vxlapi.XL_CAN_STD)
                    except VectorError as exc:
                        LOG.warning("Could not set filters: %s", exc)
            else:
                LOG.warning("Only one filter per extended or standard ID allowed")

    def recv(self, timeout=None):
        end_time = time.time() + timeout if timeout is not None else None

        if self.fd:
            event = vxlapi.XLcanRxEvent()
        else:
            event = vxlapi.XLevent()
            event_count = ctypes.c_uint()
            
        while True:
            if self.fd:
                try:
                    vxlapi.xlCanReceive(self.port_handle, event)
                except VectorError as exc:
                    if exc.error_code != vxlapi.XL_ERR_QUEUE_IS_EMPTY:
                        raise
                else:
                    if event.tag == vxlapi.XL_CAN_EV_TAG_RX_OK or event.tag == vxlapi.XL_CAN_EV_TAG_TX_OK:
                        msg_id = event.tagData.canRxOkMsg.canId
                        dlc = self.map_dlc(event.tagData.canRxOkMsg.dlc)
                        flags = event.tagData.canRxOkMsg.msgFlags
                        timestamp = event.timeStamp * 1e-9
                        msg = Message(
                            timestamp=timestamp + self._time_offset,
                            arbitration_id=msg_id & 0x1FFFFFFF,
                            extended_id=bool(msg_id & vxlapi.XL_CAN_EXT_MSG_ID),
                            is_remote_frame=bool(flags & vxlapi.XL_CAN_RXMSG_FLAG_RTR),
                            is_error_frame=bool(flags & vxlapi.XL_CAN_RXMSG_FLAG_EF),
                            is_fd=True,
                            error_state_indicator=bool(flags & vxlapi.XL_CAN_RXMSG_FLAG_ESI),
                            #extended data length (XL_CAN_RXMSG_FLAG_EDL) not in msg class
                            bitrate_switch=bool(flags & vxlapi.XL_CAN_RXMSG_FLAG_BRS),
                            dlc=dlc,
                            data=event.tagData.canRxOkMsg.data[:dlc],
                            channel=event.chanIndex)
                        return msg
            else:
                event_count.value = 1
                try:
                    vxlapi.xlReceive(self.port_handle, event_count, event)
                except VectorError as exc:
                    if exc.error_code != vxlapi.XL_ERR_QUEUE_IS_EMPTY:
                        raise
                else:
                    if event.tag == vxlapi.XL_RECEIVE_MSG:
                        msg_id = event.tagData.msg.id
                        dlc = event.tagData.msg.dlc
                        flags = event.tagData.msg.flags
                        timestamp = event.timeStamp * 1e-9
                        msg = Message(
                            timestamp=timestamp + self._time_offset,
                            arbitration_id=msg_id & 0x1FFFFFFF,
                            extended_id=bool(msg_id & vxlapi.XL_CAN_EXT_MSG_ID),
                            is_remote_frame=bool(flags & vxlapi.XL_CAN_MSG_FLAG_REMOTE_FRAME),
                            is_error_frame=bool(flags & vxlapi.XL_CAN_MSG_FLAG_ERROR_FRAME),
                            is_fd=False,
                            dlc=dlc,
                            data=event.tagData.msg.data[:dlc],
                            channel=event.chanIndex)
                        return msg

            if end_time is not None and time.time() > end_time:
                return None

            if HAS_EVENTS:
                # Wait for receive event to occur
                if timeout is None:
                    time_left_ms = INFINITE
                else:
                    time_left = end_time - time.time()
                    time_left_ms = max(0, int(time_left * 1000))
                WaitForSingleObject(self.event_handle.value, time_left_ms)
            else:
                # Wait a short time until we try again
                time.sleep(self.poll_interval)

    def send(self, msg, timeout=None):
        msg_id = msg.arbitration_id

        if msg.id_type:
            msg_id |= vxlapi.XL_CAN_EXT_MSG_ID

        flags = 0

        if self.fd:
            if msg.dlc > vxlapi.MAX_MSG_LEN: #extended data length (XL_CAN_RXMSG_FLAG_EDL) not in msg class
                flags |= vxlapi.XL_CAN_TXMSG_FLAG_EDL
            if msg.bitrate_switch:
                flags |= vxlapi.XL_CAN_TXMSG_FLAG_BRS
            if msg.is_remote_frame:
                flags |= vxlapi.XL_CAN_TXMSG_FLAG_RTR
                        
            message_count = 1
            MsgCntSent = ctypes.c_uint(1)
            
            XLcanTxEvent = vxlapi.XLcanTxEvent()
            XLcanTxEvent.tag = vxlapi.XL_CAN_EV_TAG_TX_MSG
            XLcanTxEvent.transId = 0xffff
            
            XLcanTxEvent.tagData.canMsg.canId = msg_id
            XLcanTxEvent.tagData.canMsg.msgFlags = flags
            XLcanTxEvent.tagData.canMsg.dlc = msg.dlc
            for idx, value in enumerate(msg.data):
                XLcanTxEvent.tagData.canMsg.data[idx] = value
            vxlapi.xlCanTransmitEx(self.port_handle, self.mask, message_count, MsgCntSent, XLcanTxEvent)

        else:
            if msg.is_remote_frame:
                flags |= vxlapi.XL_CAN_MSG_FLAG_REMOTE_FRAME

            message_count = ctypes.c_uint(1)
            
            xl_event = vxlapi.XLevent()
            xl_event.tag = vxlapi.XL_TRANSMIT_MSG
            
            xl_event.tagData.msg.id = msg_id
            xl_event.tagData.msg.dlc = msg.dlc
            xl_event.tagData.msg.flags = flags
            for idx, value in enumerate(msg.data):
                xl_event.tagData.msg.data[idx] = value
            vxlapi.xlCanTransmit(self.port_handle, self.mask, message_count, xl_event)

        
    def flush_tx_buffer(self):
        vxlapi.xlCanFlushTransmitQueue(self.port_handle, self.mask)

    def shutdown(self):
        vxlapi.xlDeactivateChannel(self.port_handle, self.mask)
        vxlapi.xlClosePort(self.port_handle)
        vxlapi.xlCloseDriver()
        
    def reset(self):
        vxlapi.xlDeactivateChannel(self.port_handle, self.mask)
        vxlapi.xlActivateChannel(self.port_handle, self.mask,
                                     vxlapi.XL_BUS_TYPE_CAN, 0)

    def map_dlc(self,dlc_in):
        can_fd_dlc = [0,1,2,3,4,5,6,7,8,12,16,20,24,32,48,64]
        return can_fd_dlc[dlc_in]
        
