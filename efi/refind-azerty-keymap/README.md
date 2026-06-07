# rEFInd AZERTY HII Keymap

This directory builds EFI artifacts for the rEFInd AZERTY keymap design:

- `BOOTX64.EFI`: interactive USB probe. It prints key values before and after
  `SetKeyboardLayout()`.
- `homelab_fr_azerty_x64.efi`: no-UI layout installer for rEFInd to load from
  `EFI/refind/drivers_x64/`.
- `BOOTAA64.EFI`: aarch64 interactive USB probe.
- `homelab_fr_azerty_aa64.efi`: aarch64 no-UI layout installer for
  `EFI/refind/drivers_aa64/`.

Neither artifact modifies firmware variables. The HII layout remains a runtime
pre-boot setting.

## Build

```bash
./build.sh
```

That preserves the original x86_64 outputs. Build both supported
architectures with:

```bash
./build.sh all
```

The script expects `clang` plus either `lld-link` or `ld.lld`. It searches
`PATH` first, then the Homebrew LLVM/lld locations.

Outputs:

```text
build/BOOTX64.EFI
build/homelab_fr_azerty_x64.efi
build/x86_64/BOOTX64.EFI
build/x86_64/homelab_fr_azerty_x64.efi
build/aarch64/BOOTAA64.EFI
build/aarch64/homelab_fr_azerty_aa64.efi
```

CI publishes the architecture directories, plus their `.sha256sum` files, to
the Nexus `zbm` raw repository under:

```text
refind-azerty-keymap/<VERSION>/
```

## USB layout

Copy the generated file to a FAT-formatted USB stick:

```text
EFI/BOOT/BOOTX64.EFI
```

Boot the host from that USB stick.

## Test

Before activating the HII layout, press the physical AZERTY `A`, `Q`, `Z`, `W`,
and `2/e acute` keys. Press Enter to register and activate the test layout, then
press the same keys again.

Success signal after `SetKeyboardLayout()`:

```text
physical A -> Unicode 0x0061
physical Q -> Unicode 0x0071
physical Z -> Unicode 0x007A
physical W -> Unicode 0x0077
physical 2/e acute -> Unicode 0x00E9
```

If the values after `SetKeyboardLayout()` are unchanged from before, the
firmware ignores runtime HII keyboard layouts and the rEFInd-driver approach is
not viable for that machine.

## rEFInd layout artifact

Install the no-UI artifact here:

```text
EFI/refind/drivers_x64/homelab_fr_azerty_x64.efi
EFI/refind/drivers_aa64/homelab_fr_azerty_aa64.efi
```

rEFInd loads the architecture-specific `drivers_*/*.efi` directory before
presenting its menu, so the artifact registers the HII keyboard layout and
makes it current before the kernel args editor runs.

The current mapping targets the common French PC AZERTY layout:

```text
top row:     ² & é " ' ( - è _ ç à ) =
shift row:   ² 1 2 3 4 5 6 7 8 9 0 ° +
AltGr row:     ~ # { [ | ` \ ^ @ ] }
upper row:   a z e r t y u i o p ^ $ *
home row:    q s d f g h j k l m ù *
bottom row:  < w x c v b n , ; : !
```

Dead-key behavior is intentionally not modeled. Accent keys emit visible
characters immediately (`^`, diaeresis, backtick), because rEFInd's editor is
for recovery kernel arguments and should not keep hidden composition state.
