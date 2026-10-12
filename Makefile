obj-m += omen_rgb.o
omen_rgb-y := module/omen_rgb.o

KDIR ?= /lib/modules/$(if $(KERNELRELEASE),$(KERNELRELEASE),$(shell uname -r))/build
PWD := $(shell pwd)

.PHONY: all clean test broker broker-rate-experiment broker-rate-campaign-2 broker-rate-campaign-3

all:
	$(MAKE) -C "$(KDIR)" M="$(PWD)" modules

broker: build/okeylitctl-broker

# Deliberately not a dependency of all, broker, test, or the installer.
# Review and explicit user approval are required before any laptop use.
broker-rate-experiment: build/okeylitctl-broker-rate-experiment

build/okeylitctl-broker-rate-experiment: broker/main.c
	mkdir -p build
	$(CC) -std=c11 -O2 -Wall -Wextra -Werror -DBROKER_RATE_EXPERIMENT $< -o $@

broker-rate-campaign-2: build/okeylitctl-broker-rate-campaign-2

build/okeylitctl-broker-rate-campaign-2: broker/main.c
	mkdir -p build
	$(CC) -std=c11 -O2 -Wall -Wextra -Werror -DBROKER_RATE_EXPERIMENT -DBROKER_RATE_CAMPAIGN_2 $< -o $@

broker-rate-campaign-3: build/okeylitctl-broker-rate-campaign-3

build/okeylitctl-broker-rate-campaign-3: broker/main.c
	mkdir -p build
	$(CC) -std=c11 -O2 -Wall -Wextra -Werror -DBROKER_RATE_EXPERIMENT -DBROKER_RATE_CAMPAIGN_3 $< -o $@

build/okeylitctl-broker: broker/main.c
	mkdir -p build
	$(CC) -std=c11 -O2 -Wall -Wextra -Werror -DBROKER_BOUNDED_NORMAL $< -o $@

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
