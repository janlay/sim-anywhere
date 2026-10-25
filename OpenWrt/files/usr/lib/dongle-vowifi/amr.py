#!/usr/bin/env python3
"""ctypes wrapper for opencore-amr (AMR-NB 12.2 kbit/s, 8 kHz, 20 ms)."""

import ctypes
import os
import struct

LIB = "/usr/lib/libopencore-amrnb.so.0"
if not os.path.exists(LIB):
    LIB = "/root/vowifi-probe/lib/libopencore-amrnb.so.0"
MR122 = 7  # enum Mode: MR122 = 12.2 kbit/s
FRAME_SAMPLES = 160
FRAME_BYTES = 32  # TOC + 31 speech bytes


class AmrNB:
    def __init__(self, path=LIB):
        self.lib = ctypes.CDLL(path)
        self.lib.Encoder_Interface_init.restype = ctypes.c_void_p
        self.lib.Encoder_Interface_init.argtypes = [ctypes.c_int]
        self.lib.Encoder_Interface_Encode.restype = ctypes.c_int
        self.lib.Encoder_Interface_Encode.argtypes = [
            ctypes.c_void_p, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_int,
        ]
        self.lib.Encoder_Interface_exit.restype = None
        self.lib.Encoder_Interface_exit.argtypes = [ctypes.c_void_p]
        self.lib.Decoder_Interface_init.restype = ctypes.c_void_p
        self.lib.Decoder_Interface_init.argtypes = []
        self.lib.Decoder_Interface_Decode.restype = None
        self.lib.Decoder_Interface_Decode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
        ]
        self.lib.Decoder_Interface_exit.restype = None
        self.lib.Decoder_Interface_exit.argtypes = [ctypes.c_void_p]
        self.enc = self.lib.Encoder_Interface_init(0)
        self.dec = self.lib.Decoder_Interface_init()
        if not self.enc or not self.dec:
            raise RuntimeError("AMR encoder/decoder init failed")

    def close(self):
        if self.enc:
            self.lib.Encoder_Interface_exit(self.enc)
            self.enc = None
        if self.dec:
            self.lib.Decoder_Interface_exit(self.dec)
            self.dec = None

    def encode(self, pcm):
        """pcm: 320 bytes (160 int16 little-endian) -> AMR frame (TOC + speech)."""
        if len(pcm) != FRAME_SAMPLES * 2:
            raise ValueError("pcm must be 320 bytes")
        speech = (ctypes.c_int16 * FRAME_SAMPLES).from_buffer_copy(pcm)
        out = (ctypes.c_uint8 * 64)()
        n = self.lib.Encoder_Interface_Encode(self.enc, MR122, speech, out, 1)
        if n <= 0:
            raise RuntimeError("AMR encode failed")
        return bytes(out[:n])

    def decode(self, frame):
        """AMR frame (TOC + speech) -> 320 bytes (160 int16 little-endian)."""
        if not frame:
            return b"\x00" * (FRAME_SAMPLES * 2)
        inp = (ctypes.c_uint8 * len(frame)).from_buffer_copy(frame)
        out = (ctypes.c_int16 * FRAME_SAMPLES)()
        self.lib.Decoder_Interface_Decode(self.dec, inp, out, 0)
        return bytes(out)


def silence_pcm():
    return b"\x00" * (FRAME_SAMPLES * 2)


def sine_pcm(freq=440.0, amp=8000, phase=0.0):
    import math
    out = bytearray()
    for i in range(FRAME_SAMPLES):
        v = int(amp * math.sin(2 * math.pi * freq * (i / 8000.0) + phase))
        out += struct.pack("<h", v)
    return bytes(out)
