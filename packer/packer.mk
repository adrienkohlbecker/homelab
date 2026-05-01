PACKER_DIR  := $(dir $(lastword $(MAKEFILE_LIST)))
UBUNTU_NAME ?= jammy
OUTPUT_DIR  := /mnt/qemu/$(UBUNTU_NAME)

VARIANTS := ubuntu-base ubuntu-box ubuntu-lab ubuntu-pug

PACKER_VARS := -var ubuntu_name=$(UBUNTU_NAME) -var output_directory=$(OUTPUT_DIR)

.PHONY: all clean $(VARIANTS)

all:
	$(MAKE) ubuntu-base
	$(MAKE) ubuntu-box ubuntu-lab ubuntu-pug

$(VARIANTS):
	rm -rf $(OUTPUT_DIR)/$@.new
	packer build $(PACKER_VARS) -timestamp-ui -warn-on-undeclared-var --on-error=ask -only='qemu.$@' $(PACKER_DIR)

clean:
	rm -rf $(OUTPUT_DIR)/*.new
