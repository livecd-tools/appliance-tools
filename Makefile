VERSION = 011.1

INSTALL = /usr/bin/install -c
INSTALL_PROGRAM = ${INSTALL}
INSTALL_DATA = ${INSTALL} -m 644
INSTALL_SCRIPT = ${INSTALL_PROGRAM}
PYTHON = python
PYTHON_PROGRAM = $(shell which $(PYTHON))
SED_PROGRAM = /usr/bin/sed

INSTALL_PYTHON = $(INSTALL) -m 644
define COMPILE_PYTHON
	$(PYTHON_PROGRAM) -c "import compileall as c; c.compile_dir('$(1)', force=1)"
	$(PYTHON_PROGRAM) -O -c "import compileall as c; c.compile_dir('$(1)', force=1)"
endef
PYTHONDIR := $(shell $(PYTHON_PROGRAM) -c "from __future__ import print_function; from distutils.sysconfig import get_python_lib; print(get_python_lib())")

all:

man:
	pod2man --section=8 --release="appliance-tools $(VERSION)" --center "Appliance Tools" docs/appliance-creator.pod > docs/appliance-creator.8
	pod2man --section=8 --release="appliance-tools $(VERSION)" --center "Appliance Tools" docs/ec2-converter.pod > docs/ec2-converter.8
install: man
	$(INSTALL_PROGRAM) -D tools/appliance-creator $(DESTDIR)/usr/bin/appliance-creator
	$(INSTALL_PROGRAM) -D tools/ec2-converter $(DESTDIR)/usr/bin/ec2-converter
	$(INSTALL_DATA) -D README $(DESTDIR)/usr/share/doc/appliance-tools/README
	$(INSTALL_DATA) -D COPYING $(DESTDIR)/usr/share/doc/appliance-tools/COPYING
	mkdir -p $(DESTDIR)/usr/share/appliance-tools/
	mkdir -p $(DESTDIR)/$(PYTHONDIR)/appcreate
	mkdir -p $(DESTDIR)/$(PYTHONDIR)/ec2convert
	$(INSTALL_PYTHON) -D appcreate/*.py $(DESTDIR)/$(PYTHONDIR)/appcreate/
	$(INSTALL_PYTHON) -D ec2convert/*.py $(DESTDIR)/$(PYTHONDIR)/ec2convert/
	$(call COMPILE_PYTHON,$(DESTDIR)/$(PYTHONDIR)/appcreate)
	$(call COMPILE_PYTHON,$(DESTDIR)/$(PYTHONDIR)/ec2convert)
	mkdir -p $(DESTDIR)/usr/share/man/man8
	$(INSTALL_DATA) -D docs/*.8 $(DESTDIR)/usr/share/man/man8
	$(SED_PROGRAM) -i "s:#!/usr/bin/python:#!$(PYTHON_PROGRAM):g" $(DESTDIR)/usr/bin/appliance-creator
	$(SED_PROGRAM) -i "s:#!/usr/bin/python:#!$(PYTHON_PROGRAM):g" $(DESTDIR)/usr/bin/ec2-converter

uninstall:
	rm -f $(DESTDIR)/usr/bin/appliance-creator
	rm -f $(DESTDIR)/usr/bin/ec2-converter
	rm -rf $(DESTDIR)/usr/lib/appliance-creator
	rm -rf $(DESTDIR)/usr/share/doc/appliance-tools-$(VERSION)

dist : all
	git archive --format=tar --prefix=appliance-tools-$(VERSION)/ HEAD | bzip2 -9v > appliance-tools-$(VERSION).tar.bz2

clean:
	rm -f *~ creator/*~ installer/*~ docs/*.8

