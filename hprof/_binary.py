from datetime import datetime
from mmap import mmap, MAP_PRIVATE, PROT_READ

import builtins
import struct

from ._dump import Dump
from ._errors import *
from ._offset import offset
from .record import create, HeapDumpSegment, HeapDumpEnd, Utf8, ClassLoad
from ._types import JavaType

_jtlookup = {}
for _jt in JavaType:
	_jtlookup[_jt.value] = _jt

def open(path):
	'''open an hprof file'''
	return HprofFile(path)

GEN_LEVEL_NAMES = 1
GEN_LEVEL_CINFO = 2
GEN_LEVEL_DUMPS = 3

class HprofFile(object):
	'''This object is your entry point into the hprof file.'''

	def __init__(self, data):
		''' data may be a file path or just plain bytes. '''
		if type(data) is bytes:
			self._f = None
			self._data = data
		elif type(data) is str:
			self._f = builtins.open(data, 'rb')
			self._data = mmap(self._f.fileno(), 0, MAP_PRIVATE, PROT_READ);
		else:
			raise TypeError(type(data))

		ident = self.read_bytes(0, 13)
		if ident != b'JAVA PROFILE ':
			raise FileFormatError('bad header: expected JAVA PROFILE, but found %s' % repr(ident))
		version = self.read_ascii(13)
		accepted_versions = ('1.0.2', '1.0.3')
		if version not in accepted_versions:
			raise FileFormatError('bad version %s; expected one of %s' % (version, ', '.join(accepted_versions)))
		base = 13 + len(version) + 1

		self.idsize = self.read_uint(base)
		timestamp_ms = (self.read_uint(base + 4) << 32) + self.read_uint(base + 8)
		self.starttime = datetime.fromtimestamp(timestamp_ms / 1000)
		self._first_record_addr = base + 12
		self._dumps = None
		self._names = {}
		self._class_info = {}
		self._caches_built = 0

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		self.close()

	def _gen_from_records(self, genlevel):
		''' traversing all the records can take a while; try to generate several things at once '''
		assert genlevel <= GEN_LEVEL_DUMPS
		assert self._caches_built <= GEN_LEVEL_DUMPS
		if self._caches_built >= genlevel:
			return # nothing to do

		# our caches may be empty or partially-built; restart.
		if self._caches_built < GEN_LEVEL_NAMES:
			self._names.clear()
		if self._caches_built < GEN_LEVEL_CINFO:
			self._class_info.clear()
		if self._caches_built < GEN_LEVEL_DUMPS:
			assert self._dumps == None

		curdump = None
		dumps = []
		for r in self.records():
			if self._caches_built < GEN_LEVEL_NAMES <= genlevel and type(r) is Utf8:
				nameid = r.id
				if nameid in self._names:
					old = self._names[nameid]
					raise FileFormatError(
							'duplicate name id 0x%x: "%s" at 0x%x and "%s" at 0x%x'
							% (nameid, old.str, old.hprof_addr, r.str, r.hprof_addr))
				self._names[nameid] = r
			elif self._caches_built < GEN_LEVEL_CINFO <= genlevel and type(r) is ClassLoad:
				cname = r.class_name
				cid   = r.class_id
				# TODO: dupes aren't necessarily wrong... but I've never seen any unloads in hprofs.
				if cname in self._class_info:
					raise FileFormatError('duplicate class load of name %s' % cname)
				if cid in self._class_info:
					raise FileFormatError('duplicate class object id 0x%x' % cid)
				self._class_info[cname] = r
				self._class_info[cid]   = r
			elif self._caches_built < GEN_LEVEL_DUMPS <= genlevel and type(r) is HeapDumpSegment:
				if curdump is None:
					curdump = Dump(self)
					dumps.append(curdump)
				curdump._add_segment(r)
			elif self._caches_built < GEN_LEVEL_DUMPS <= genlevel and type(r) is HeapDumpEnd:
				if curdump is None:
					dumps.append(Dump(self))
				curdump = None
		if GEN_LEVEL_DUMPS <= genlevel:
			self._dumps = tuple(dumps)
		self._caches_built = genlevel

	def records(self):
		'''yield all top-level records from this file.'''
		addr = self._first_record_addr
		while True:
			try:
				tag = self.read_byte(addr)
			except EofError:
				break # alright, everything lined up nicely!
			r = create(self, addr)
			addr += r._hprof_len # skip to the next record
			yield r

	def dumps(self):
		'''yield hprof.Dump objects representing the memory dumps present in this file.

		Dumps allow convenient exploration of objects, but may be a bit slow to initialize.'''
		if self._dumps is None:
			self._gen_from_records(GEN_LEVEL_DUMPS)
		yield from self._dumps

	def close(self):
		''' close the hprof file '''
		if self._data is not None:
			if type(self._data) is mmap:
				self._data.close()
			self._data = None
		if self._f is not None:
			self._f.close()
			self._f = None

	def _cache_lookup(self, which, key, genlevel):
		cache = getattr(self, which)
		try:
			return cache[key]
		except KeyError:
			pass
		self._gen_from_records(genlevel)
		return cache[key]

	def name(self, nameid):
		'''look up a name record by name ID.'''
		try:
			return self._cache_lookup('_names', nameid, GEN_LEVEL_NAMES)
		except KeyError:
			raise RefError('name', nameid)

	def get_class_info(self, class_id_or_name):
		'''return the hprof.record.ClassLoad record for the provided class object ID or name.'''
		try:
			return self._cache_lookup('_class_info', class_id_or_name, GEN_LEVEL_CINFO)
		except KeyError:
			pass
		if type(class_id_or_name) is int:
			class_id_or_name = hex(class_id_or_name)
		raise ClassNotFoundError('ClassLoad record for class id %s' % class_id_or_name)

	def _read_bytes(self, start, nbytes):
		if start < 0:
			raise EofError('tried to read at address %d' % start)
		length = len(self._data)
		if nbytes is not None:
			if nbytes < 0:
				raise ValueError('invalid nbytes', nbytes)
			end = start + nbytes
			if end > length:
				raise EofError('tried to read bytes %d:%d, but file size is %d' % (start, end, length))
		else:
			end = start
			while end < length:
				if self._data[end] == 0:
					break
				end += 1
			else:
				raise EofError('tried to read from %d to null termination, but exceeded file size %d' % (start, length))
		return self._data[start:end]

	def read_bytes(self, addr, nbytes):
		''' Read a byte string of nbytes. '''
		return self._read_bytes(addr, nbytes)

	def read_ascii(self, addr, nbytes=None):
		''' Read an ascii string of nbytes. If nbytes is None, read until a zero byte is found. '''
		return self._read_bytes(addr, nbytes).decode('ascii')

	def read_utf8(self, addr, nbytes):
		''' Read an utf8 string of nbytes. Note: byte count, not character count! '''
		return self._read_bytes(addr, nbytes).decode('utf8')

	def read_jtype(self, addr):
		'''Read a byte and return it as an hprof.JavaType value.'''
		b = self.read_byte(addr)
		try:
			return _jtlookup[b]
		except KeyError as e:
			raise FileFormatError('invalid JavaType: 0x%x' % b)

	def read_jvalue(self, addr, jtype):
		'''Read a java value of the specified type.'''
		readers = {
			JavaType.object:  self.read_id,
			JavaType.boolean: self.read_boolean,
			JavaType.byte:    self.read_byte,
			JavaType.char:    self.read_char,
			JavaType.short:   self.read_short,
			JavaType.int:     self.read_int,
			JavaType.long:    self.read_long,
			JavaType.float:   self.read_float,
			JavaType.double:  self.read_double,
		}
		try:
			rfun = readers[jtype]
		except KeyError:
			raise Error('unhandled (or invalid) JavaType: %s' % jtype)
		return rfun(addr)

	def read_char(self, addr):
		'''Read a single java char at the specified address.'''
		return self._read_bytes(addr, 2).decode('utf-16-be')

	def read_byte(self, addr):
		'''Read a single unsigned byte at the specified address.'''
		if addr < 0:
			raise EofError('tried to read at address %d' % addr)
		try:
			return self._data[addr]
		except IndexError:
			raise EofError('tried to read bytes %d:%d, but file size is %d' % (addr, addr+1, len(self._data)))

	def read_uint(self, addr):
		'''Read an unsigned 32-bit integer at the specified address.'''
		v, = struct.unpack('>I', self._read_bytes(addr, 4))
		return v

	def read_int(self, addr):
		'''Read a signed 32-bit integer at the specified address.'''
		v, = struct.unpack('>i', self._read_bytes(addr, 4))
		return v

	def read_ushort(self, addr):
		'''Read an unsigned 16-bit integer at the specified address.'''
		v, = struct.unpack('>H', self._read_bytes(addr, 2))
		return v

	def read_short(self, addr):
		'''Read a signed 16-bit integer at the specified address.'''
		v, = struct.unpack('>h', self._read_bytes(addr, 2))
		return v

	def read_boolean(self, addr):
		'''Read a boolean value at the specified address.'''
		b, = self._read_bytes(addr, 1)
		if b == 0:
			return False
		elif b == 1:
			return True
		else:
			raise FileFormatError('invalid boolean value 0x%x' % b)

	def read_id(self, addr):
		'''Read an id (i.e. an object or name reference) at the specified address.'''
		bytes = self._read_bytes(addr, self.idsize)
		i = 0
		for b in bytes:
			i = (i << 8) + b
		return i

	def read_float(self, addr):
		'''Read a 32-bit float value at the specified address.'''
		v, = struct.unpack('>f', self._read_bytes(addr, 4))
		return v

	def read_double(self, addr):
		'''Read a 64-bit double value at the specified address.'''
		v, = struct.unpack('>d', self._read_bytes(addr, 8))
		return v

	def read_long(self, addr):
		'''Read a signed 64-bit integer at the specified address.'''
		v, = struct.unpack('>q', self._read_bytes(addr, 8))
		return v
