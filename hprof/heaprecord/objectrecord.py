#!/usr/bin/env python3
#coding=utf8

from .heaprecord import HeapRecord

from ..offset import offset, AutoOffsets, idoffset



class ObjectRecord(HeapRecord):
	TAG = 0x21

	_offsets = AutoOffsets(1,
		'ID',       idoffset(1),
		'STRACE',   4,
		'CLSID',    idoffset(1),
		'DATASIZE', 4,
		'DATA'
	)

	@property
	def id(self):
		return self._read_id(self._off.ID)

	@property
	def datalen(self):
		return self._read_uint(self._off.DATASIZE)

	def __len__(self):
		return self._off.DATA + self.datalen

	def __str__(self):
		return 'ObjectRecord(id=0x%x)' % self.id
