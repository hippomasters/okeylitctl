obj-m += omen_rgb.o
omen_rgb-y := module/omen_rgb.o

KDIR ?= /lib/modules/$(if $(KERNELRELEASE),$(KERNELRELEASE),$(shell uname -r))/build
PWD := $(shell pwd)

.PHONY: all clean test

all:
	$(MAKE) -C "$(KDIR)" M="$(PWD)" modules

clean:
	@if [ -d "$(KDIR)" ]; then $(MAKE) -C "$(KDIR)" M="$(PWD)" clean; fi
	$(RM) *.ko *.o *.mod *.mod.c Module.symvers modules.order
	$(RM) module/*.o module/*.mod module/*.mod.c
	$(RM) .*.cmd module/.*.cmd
	$(RM) -r build

test:
	mkdir -p build
	$(CC) -std=c11 -Wall -Wextra -Werror tests/test_protocol.c -o build/test_protocol
	./build/test_protocol
	PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py' -v
