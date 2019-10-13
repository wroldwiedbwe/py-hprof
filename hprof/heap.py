class Heap(object):
	__slots__ = ('objects')

	def __init__(self):
		self.objects = {}


class Ref(object):
	__slots__ = ('_target', '_reftype')

	def __new__(cls, target, reftype):
		# refs to refs aren't allowed
		if type(target) is Ref:
			target = Ref._target.__get__(target)

		# ...and no indirection when we just want the exact type
		if reftype is None or type(target) is reftype:
			return target

		# can't cast to just anything
		if not isinstance(target, reftype):
			raise TypeError('%r is not an instance of %r' % (target, reftype))

		# ...and refs to classes don't make sense.
		if isinstance(target, JavaClass):
			return target

		ref = super().__new__(cls)
		ref._target = target
		ref._reftype = reftype
		return ref

	def __getattribute__(self, name):
		t = Ref._target.__get__(self)
		r = Ref._reftype.__get__(self)
		return t.__getattr__(name, r)

	def __dir__(self):
		t = Ref._target.__get__(self)
		r = Ref._reftype.__get__(self)
		return t.__dir__(r)

	def __repr__(self):
		t = Ref._target.__get__(self)
		r = Ref._reftype.__get__(self)
		objid = JavaObject._hprof_id.__get__(t)
		return '<Ref of type %s to %s 0x%x>' % (r, type(t), objid)

	def __eq__(self, other):
		t = Ref._target.__get__(self)
		return t == other


def cast(obj, desired=None):
	return Ref(obj, desired)


class JavaClassContainer(str):
	pass

class JavaPackage(JavaClassContainer):
	''' a Java package, containing JavaClassName objects '''
	def __repr__(self):
		return "<JavaPackage '%s'>" % self


class JavaClassName(JavaClassContainer):
	''' a Java class name that can be used to look up JavaClass objects.
	    May contain nested JavaClassName objects. '''

	def __repr__(self):
		return "<JavaClassName '%s'>" % self


class JavaObject(object):
	__slots__ = (
		'_hprof_id',       # object id
	)

	def __init__(self, objid):
		JavaObject._hprof_id.__set__(self, objid)

	def __repr__(self):
		objid = JavaObject._hprof_id.__get__(self)
		return '<%s 0x%x>' % (type(self), objid)

	def __dir__(self, reftype=None):
		out = set()
		if reftype is None:
			t = type(self)
		else:
			t = reftype
		while t is not JavaObject:
			out.update(t._hprof_ifields.keys())
			out.update(t._hprof_sfields.keys())
			t, = t.__bases__
		return tuple(out)

	def __getattr__(self, name, reftype=None):
		if reftype is None:
			t = type(self)
		else:
			t = reftype
		while t is not JavaObject:
			if name in t._hprof_ifields:
				ix = t._hprof_ifields[name]
				vals = t._hprof_ifieldvals.__get__(self)
				return vals[ix]
			elif name in t._hprof_sfields:
				return t._hprof_sfields[name]
			t, = t.__bases__
		# TODO: implement getattr(x, 'super') to return a Ref?
		# TODO: ...and x.SuperClass too?
		raise AttributeError('type %r has no attribute %r' % (type(self), name))

	def __len__(self):
		if not isinstance(type(self), JavaArrayClass):
			raise TypeError('%r object has no len()' % type(self))
		return len(self._hprof_array_data)

	def __getitem__(self, ix):
		if not isinstance(type(self), JavaArrayClass):
			raise TypeError('%r is not an array type' % type(self))
		return self._hprof_array_data[ix]

class JavaClass(type):
	__slots__ = ()

	def __new__(meta, name, supercls, instance_attrs):
		assert '.' not in name
		assert '/' not in name or name.find('/') >= name.find('$$')
		assert '$' not in name or name.find('$') >= name.find('$$')
		assert ';' not in name
		if supercls is None:
			supercls = JavaObject
		if meta is JavaArrayClass and not isinstance(supercls, JavaArrayClass):
			slots = ('_hprof_ifieldvals', '_hprof_array_data')
		else:
			slots = ('_hprof_ifieldvals')
		cls = super().__new__(meta, name, (supercls,), {
			'__slots__': slots,
		})
		cls._hprof_sfields = dict()
		cls._hprof_ifields = dict()
		for ix, field in enumerate(instance_attrs):
			cls._hprof_ifields[field] = ix
		return cls

	def __init__(meta, name, supercls, instance_attrs):
		super().__init__(name, None, None)

	def __str__(self):
		if self.__module__:
			return self.__module__ + '.' + self.__name__
		return self.__name__

	def __repr__(self):
		return "<JavaClass '%s'>" % str(self)

	def __instancecheck__(cls, instance):
		if type(instance) is Ref:
			instance = Ref._target.__get__(instance)
		if type(instance) is JavaClass:
			# not pretty...
			if str(cls) in ('java.lang.Object', 'java.lang.Class'):
				return True
		return super().__instancecheck__(instance)

	def __getattr__(self, name):
		t = self
		while t is not JavaObject:
			if name in t._hprof_sfields:
				return t._hprof_sfields[name]
			t, = t.__bases__
		raise AttributeError('type %r has no static attribute %r' % (self, name))


class JavaArrayClass(JavaClass):
	__slots__ = ()


def _get_or_create_container(container, parts, ctype):
	for p in parts:
		assert p
		assert '.' not in p
		assert ';' not in p
		assert '/' not in p or p.find('/') >= p.find('$$')
		assert '$' not in p or p.find('$') >= p.find('$$')
		if hasattr(container, p):
			container = getattr(container, p)
			assert isinstance(container, ctype)
		else:
			if isinstance(container, JavaClassContainer):
				next = ctype(str(container) + '.' + p)
			else:
				next = ctype(p)
			setattr(container, p, next)
			container = next
	return container

def _create_class(container, name, supercls, slots):
	nests = 0
	while name[nests] == '[':
		nests += 1

	if nests:
		assert name[nests] == 'L'
		assert name.endswith(';')
		name = name[nests+1:-1]

	assert '.' not in name
	assert ';' not in name
	assert '[' not in name

	# special handling for lambda names (jvm-specific name generation?)
	# in short: everything after $$ is part of the class name.
	dollars = name.find('$$')
	if dollars >= 0:
		extra = name[dollars:]
		name  = name[:dollars]
	else:
		extra = ''

	name = name.split('/')
	container = _get_or_create_container(container, name[:-1], JavaPackage)
	name = name[-1].split('$')
	if extra:
		name[-1] += extra
	name[-1] += nests * '[]'
	container = _get_or_create_container(container, name[:-1], JavaClassName)
	classname = _get_or_create_container(container, name[-1:], JavaClassName)
	name = name[-1]
	if nests:
		cls = JavaArrayClass(name, supercls, slots)
	else:
		cls = JavaClass(name, supercls, slots)
	if isinstance(container, JavaClassContainer):
		type.__setattr__(cls, '__module__', container)
	else:
		type.__setattr__(cls, '__module__', None)
	return cls
