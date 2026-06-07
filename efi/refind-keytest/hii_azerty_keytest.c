typedef unsigned char UINT8;
typedef unsigned short UINT16;
typedef unsigned int UINT32;
typedef unsigned long long UINT64;
typedef unsigned long long UINTN;
typedef unsigned long long EFI_STATUS;
typedef void *EFI_HANDLE;
typedef void *EFI_EVENT;
typedef void VOID;
typedef UINT16 CHAR16;

#define EFI_SUCCESS 0
#define EFI_ERROR(Status) (((Status) & 0x8000000000000000ULL) != 0)
#define EFIAPI __attribute__((ms_abi))
#define PACKED __attribute__((packed))
#define NULL ((void *)0)

typedef struct {
  UINT32 Data1;
  UINT16 Data2;
  UINT16 Data3;
  UINT8 Data4[8];
} EFI_GUID;

typedef struct {
  UINT64 Signature;
  UINT32 Revision;
  UINT32 HeaderSize;
  UINT32 CRC32;
  UINT32 Reserved;
} EFI_TABLE_HEADER;

typedef struct {
  UINT16 ScanCode;
  CHAR16 UnicodeChar;
} EFI_INPUT_KEY;

typedef struct _EFI_SIMPLE_TEXT_INPUT_PROTOCOL EFI_SIMPLE_TEXT_INPUT_PROTOCOL;
typedef struct _EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL;
typedef struct _EFI_BOOT_SERVICES EFI_BOOT_SERVICES;

struct _EFI_SIMPLE_TEXT_INPUT_PROTOCOL {
  EFI_STATUS(EFIAPI *Reset)(EFI_SIMPLE_TEXT_INPUT_PROTOCOL *This, UINT8 ExtendedVerification);
  EFI_STATUS(EFIAPI *ReadKeyStroke)(EFI_SIMPLE_TEXT_INPUT_PROTOCOL *This, EFI_INPUT_KEY *Key);
  EFI_EVENT WaitForKey;
};

struct _EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL {
  VOID *Reset;
  EFI_STATUS(EFIAPI *OutputString)(EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL *This, CHAR16 *String);
  VOID *TestString;
  VOID *QueryMode;
  VOID *SetMode;
  VOID *SetAttribute;
  VOID *ClearScreen;
  VOID *SetCursorPosition;
  VOID *EnableCursor;
};

struct _EFI_BOOT_SERVICES {
  EFI_TABLE_HEADER Hdr;
  VOID *RaiseTPL;
  VOID *RestoreTPL;
  VOID *AllocatePages;
  VOID *FreePages;
  VOID *GetMemoryMap;
  VOID *AllocatePool;
  VOID *FreePool;
  VOID *CreateEvent;
  VOID *SetTimer;
  EFI_STATUS(EFIAPI *WaitForEvent)(UINTN NumberOfEvents, EFI_EVENT *Event, UINTN *Index);
  VOID *SignalEvent;
  VOID *CloseEvent;
  VOID *CheckEvent;
  VOID *InstallProtocolInterface;
  VOID *ReinstallProtocolInterface;
  VOID *UninstallProtocolInterface;
  VOID *HandleProtocol;
  VOID *Reserved;
  VOID *RegisterProtocolNotify;
  VOID *LocateHandle;
  VOID *LocateDevicePath;
  VOID *InstallConfigurationTable;
  VOID *LoadImage;
  VOID *StartImage;
  VOID *Exit;
  VOID *UnloadImage;
  VOID *ExitBootServices;
  VOID *GetNextMonotonicCount;
  VOID *Stall;
  VOID *SetWatchdogTimer;
  VOID *ConnectController;
  VOID *DisconnectController;
  VOID *OpenProtocol;
  VOID *CloseProtocol;
  VOID *OpenProtocolInformation;
  VOID *ProtocolsPerHandle;
  VOID *LocateHandleBuffer;
  EFI_STATUS(EFIAPI *LocateProtocol)(EFI_GUID *Protocol, VOID *Registration, VOID **Interface);
};

typedef struct {
  EFI_TABLE_HEADER Hdr;
  CHAR16 *FirmwareVendor;
  UINT32 FirmwareRevision;
  EFI_HANDLE ConsoleInHandle;
  EFI_SIMPLE_TEXT_INPUT_PROTOCOL *ConIn;
  EFI_HANDLE ConsoleOutHandle;
  EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL *ConOut;
  EFI_HANDLE StandardErrorHandle;
  EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL *StdErr;
  VOID *RuntimeServices;
  EFI_BOOT_SERVICES *BootServices;
  UINTN NumberOfTableEntries;
  VOID *ConfigurationTable;
} EFI_SYSTEM_TABLE;

typedef VOID *EFI_HII_HANDLE;

typedef struct {
  EFI_GUID PackageListGuid;
  UINT32 PackageLength;
} PACKED EFI_HII_PACKAGE_LIST_HEADER;

typedef enum {
  EfiKeyLCtrl,
  EfiKeyA0,
  EfiKeyLAlt,
  EfiKeySpaceBar,
  EfiKeyA2,
  EfiKeyA3,
  EfiKeyA4,
  EfiKeyRCtrl,
  EfiKeyLeftArrow,
  EfiKeyDownArrow,
  EfiKeyRightArrow,
  EfiKeyZero,
  EfiKeyPeriod,
  EfiKeyEnter,
  EfiKeyLShift,
  EfiKeyB0,
  EfiKeyB1,
  EfiKeyB2,
  EfiKeyB3,
  EfiKeyB4,
  EfiKeyB5,
  EfiKeyB6,
  EfiKeyB7,
  EfiKeyB8,
  EfiKeyB9,
  EfiKeyB10,
  EfiKeyRShift,
  EfiKeyUpArrow,
  EfiKeyOne,
  EfiKeyTwo,
  EfiKeyThree,
  EfiKeyCapsLock,
  EfiKeyC1,
  EfiKeyC2,
  EfiKeyC3,
  EfiKeyC4,
  EfiKeyC5,
  EfiKeyC6,
  EfiKeyC7,
  EfiKeyC8,
  EfiKeyC9,
  EfiKeyC10,
  EfiKeyC11,
  EfiKeyC12,
  EfiKeyFour,
  EfiKeyFive,
  EfiKeySix,
  EfiKeyPlus,
  EfiKeyTab,
  EfiKeyD1,
  EfiKeyD2,
  EfiKeyD3,
  EfiKeyD4,
  EfiKeyD5,
  EfiKeyD6,
  EfiKeyD7,
  EfiKeyD8,
  EfiKeyD9,
  EfiKeyD10,
  EfiKeyD11,
  EfiKeyD12,
  EfiKeyD13,
  EfiKeyDel,
  EfiKeyEnd,
  EfiKeyPgDn,
  EfiKeySeven,
  EfiKeyEight,
  EfiKeyNine,
  EfiKeyE0,
  EfiKeyE1,
  EfiKeyE2,
  EfiKeyE3,
  EfiKeyE4,
  EfiKeyE5,
  EfiKeyE6,
  EfiKeyE7,
  EfiKeyE8,
  EfiKeyE9,
  EfiKeyE10,
  EfiKeyE11,
  EfiKeyE12,
  EfiKeyBackSpace,
  EfiKeyIns,
  EfiKeyHome,
  EfiKeyPgUp,
  EfiKeyNLck,
  EfiKeySlash,
  EfiKeyAsterisk,
  EfiKeyMinus,
  EfiKeyEsc
} EFI_KEY;

typedef struct {
  EFI_KEY Key;
  CHAR16 Unicode;
  CHAR16 ShiftedUnicode;
  CHAR16 AltGrUnicode;
  CHAR16 ShiftedAltGrUnicode;
  UINT16 Modifier;
  UINT16 AffectedAttribute;
} PACKED EFI_KEY_DESCRIPTOR;

#define EFI_NULL_MODIFIER 0x0000
#define EFI_LEFT_CONTROL_MODIFIER 0x0001
#define EFI_RIGHT_CONTROL_MODIFIER 0x0002
#define EFI_LEFT_ALT_MODIFIER 0x0003
#define EFI_RIGHT_ALT_MODIFIER 0x0004
#define EFI_INSERT_MODIFIER 0x0006
#define EFI_DELETE_MODIFIER 0x0007
#define EFI_PAGE_DOWN_MODIFIER 0x0008
#define EFI_PAGE_UP_MODIFIER 0x0009
#define EFI_HOME_MODIFIER 0x000a
#define EFI_END_MODIFIER 0x000b
#define EFI_LEFT_SHIFT_MODIFIER 0x000c
#define EFI_RIGHT_SHIFT_MODIFIER 0x000d
#define EFI_CAPS_LOCK_MODIFIER 0x000e
#define EFI_NUM_LOCK_MODIFIER 0x000f
#define EFI_LEFT_ARROW_MODIFIER 0x0010
#define EFI_RIGHT_ARROW_MODIFIER 0x0011
#define EFI_DOWN_ARROW_MODIFIER 0x0012
#define EFI_UP_ARROW_MODIFIER 0x0013
#define EFI_AFFECTED_BY_STANDARD_SHIFT 0x0001
#define EFI_AFFECTED_BY_CAPS_LOCK 0x0002
#define EFI_AFFECTED_BY_NUM_LOCK 0x0004
#define EFI_HII_PACKAGE_KEYBOARD_LAYOUT 0x09
#define EFI_HII_PACKAGE_END 0xdf

typedef struct {
  UINT16 LayoutLength;
  EFI_GUID Guid;
  UINT32 LayoutDescriptorStringOffset;
  UINT8 DescriptorCount;
} PACKED EFI_HII_KEYBOARD_LAYOUT_HEADER;

typedef struct {
  UINT32 Header;
  UINT16 LayoutCount;
  EFI_HII_KEYBOARD_LAYOUT_HEADER Layout;
  EFI_KEY_DESCRIPTOR Descriptors[89];
  CHAR16 Description[25];
} PACKED KEYBOARD_PACKAGE;

typedef struct {
  EFI_HII_PACKAGE_LIST_HEADER Header;
  KEYBOARD_PACKAGE Keyboard;
  UINT32 EndPackageHeader;
} PACKED KEYTEST_PACKAGE_LIST;

typedef struct _EFI_HII_DATABASE_PROTOCOL EFI_HII_DATABASE_PROTOCOL;
struct _EFI_HII_DATABASE_PROTOCOL {
  EFI_STATUS(EFIAPI *NewPackageList)(
      const EFI_HII_DATABASE_PROTOCOL *This,
      const EFI_HII_PACKAGE_LIST_HEADER *PackageList,
      EFI_HANDLE DriverHandle,
      EFI_HII_HANDLE *Handle);
  VOID *RemovePackageList;
  VOID *UpdatePackageList;
  VOID *ListPackageLists;
  VOID *ExportPackageLists;
  VOID *RegisterPackageNotify;
  VOID *UnregisterPackageNotify;
  VOID *FindKeyboardLayouts;
  VOID *GetKeyboardLayout;
  EFI_STATUS(EFIAPI *SetKeyboardLayout)(const EFI_HII_DATABASE_PROTOCOL *This, const EFI_GUID *KeyGuid);
  VOID *GetPackageListHandle;
};

static EFI_SYSTEM_TABLE *gST;
static EFI_BOOT_SERVICES *gBS;

static EFI_GUID gEfiHiiDatabaseProtocolGuid = {
    0xef9fc172, 0xa1b2, 0x4693, {0xb3, 0x27, 0x6d, 0x32, 0xfc, 0x41, 0x60, 0x42}};

static EFI_GUID gHomelabFrAzertyLayoutGuid = {
    0x7a77c0de, 0x2f6e, 0x4a1f, {0x9a, 0x2b, 0x48, 0x3f, 0x72, 0x1b, 0x91, 0x10}};

#define HII_HEADER(Length, Type) ((UINT32)(Length) | ((UINT32)(Type) << 24))
#define STD EFI_AFFECTED_BY_STANDARD_SHIFT
#define CAPS (EFI_AFFECTED_BY_STANDARD_SHIFT | EFI_AFFECTED_BY_CAPS_LOCK)
#define DESC_STRING_BYTES (sizeof(((KEYBOARD_PACKAGE *)0)->Description))
#define DESC_COUNT (sizeof(((KEYBOARD_PACKAGE *)0)->Descriptors) / sizeof(EFI_KEY_DESCRIPTOR))
#define LAYOUT_DESC_OFFSET (sizeof(EFI_HII_KEYBOARD_LAYOUT_HEADER) + sizeof(((KEYBOARD_PACKAGE *)0)->Descriptors))
#define LAYOUT_LENGTH (sizeof(KEYBOARD_PACKAGE) - sizeof(UINT32) - sizeof(UINT16))

static KEYTEST_PACKAGE_LIST gAzertyPackage = {
    {
        {0x5a14cdb9, 0x76b6, 0x4e40, {0x94, 0x23, 0x79, 0x7e, 0xed, 0x40, 0x41, 0x9b}},
        sizeof(KEYTEST_PACKAGE_LIST),
    },
    {
        HII_HEADER(sizeof(KEYBOARD_PACKAGE), EFI_HII_PACKAGE_KEYBOARD_LAYOUT),
        1,
        {
            LAYOUT_LENGTH,
            {0x7a77c0de, 0x2f6e, 0x4a1f, {0x9a, 0x2b, 0x48, 0x3f, 0x72, 0x1b, 0x91, 0x10}},
            LAYOUT_DESC_OFFSET,
            DESC_COUNT,
        },
        {
            // Deliberately no dead-key composition: every printable key emits
            // a character immediately. rEFInd recovery typing wants predictable
            // kernel-args input, not accent composition state.
            {EfiKeyE0, 0x00b2, 0x00b2, 0, 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyE1, '&', '1', 0, 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyE2, 0x00e9, '2', '~', 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyE3, '"', '3', '#', 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyE4, '\'', '4', '{', 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyE5, '(', '5', '[', 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyE6, '-', '6', '|', 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyE7, 0x00e8, '7', '`', 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyE8, '_', '8', '\\', 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyE9, 0x00e7, '9', '^', 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyE10, 0x00e0, '0', '@', 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyE11, ')', 0x00b0, ']', 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyE12, '=', '+', '}', 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyD1, 'a', 'A', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyD2, 'z', 'Z', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyD3, 'e', 'E', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyD4, 'r', 'R', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyD5, 't', 'T', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyD6, 'y', 'Y', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyD7, 'u', 'U', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyD8, 'i', 'I', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyD9, 'o', 'O', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyD10, 'p', 'P', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyD11, '^', 0x00a8, 0, 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyD12, '$', 0x00a3, 0x00a4, 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyD13, '*', 0x00b5, 0, 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyC1, 'q', 'Q', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyC2, 's', 'S', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyC3, 'd', 'D', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyC4, 'f', 'F', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyC5, 'g', 'G', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyC6, 'h', 'H', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyC7, 'j', 'J', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyC8, 'k', 'K', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyC9, 'l', 'L', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyC10, 'm', 'M', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyC11, 0x00f9, '%', 0, 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyC12, '*', 0x00b5, 0, 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyB0, '<', '>', 0, 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyB1, 'w', 'W', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyB2, 'x', 'X', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyB3, 'c', 'C', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyB4, 'v', 'V', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyB5, 'b', 'B', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyB6, 'n', 'N', 0, 0, EFI_NULL_MODIFIER, CAPS},
            {EfiKeyB7, ',', '?', 0, 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyB8, ';', '.', 0, 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyB9, ':', '/', 0, 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyB10, '!', 0x00a7, 0, 0, EFI_NULL_MODIFIER, STD},
            {EfiKeyTab, 0x0009, 0x0009, 0, 0, EFI_NULL_MODIFIER, 0},
            {EfiKeyBackSpace, 0x0008, 0x0008, 0, 0, EFI_NULL_MODIFIER, 0},
            {EfiKeyEnter, 0x000d, 0x000d, 0, 0, EFI_NULL_MODIFIER, 0},
            {EfiKeyEsc, 0x001b, 0x001b, 0, 0, EFI_NULL_MODIFIER, 0},
            {EfiKeySpaceBar, ' ', ' ', 0, 0, EFI_NULL_MODIFIER, 0},
            {EfiKeyCapsLock, 0, 0, 0, 0, EFI_CAPS_LOCK_MODIFIER, 0},
            {EfiKeyLeftArrow, 0, 0, 0, 0, EFI_LEFT_ARROW_MODIFIER, 0},
            {EfiKeyRightArrow, 0, 0, 0, 0, EFI_RIGHT_ARROW_MODIFIER, 0},
            {EfiKeyDownArrow, 0, 0, 0, 0, EFI_DOWN_ARROW_MODIFIER, 0},
            {EfiKeyUpArrow, 0, 0, 0, 0, EFI_UP_ARROW_MODIFIER, 0},
            {EfiKeyDel, 0, 0, 0, 0, EFI_DELETE_MODIFIER, 0},
            {EfiKeyEnd, 0, 0, 0, 0, EFI_END_MODIFIER, 0},
            {EfiKeyPgDn, 0, 0, 0, 0, EFI_PAGE_DOWN_MODIFIER, 0},
            {EfiKeyIns, 0, 0, 0, 0, EFI_INSERT_MODIFIER, 0},
            {EfiKeyHome, 0, 0, 0, 0, EFI_HOME_MODIFIER, 0},
            {EfiKeyPgUp, 0, 0, 0, 0, EFI_PAGE_UP_MODIFIER, 0},
            {EfiKeyNLck, 0, 0, 0, 0, EFI_NUM_LOCK_MODIFIER, 0},
            {EfiKeySlash, '/', '/', 0, 0, EFI_NULL_MODIFIER, 0},
            {EfiKeyAsterisk, '*', '*', 0, 0, EFI_NULL_MODIFIER, 0},
            {EfiKeyMinus, '-', '-', 0, 0, EFI_NULL_MODIFIER, 0},
            {EfiKeyPlus, '+', '+', 0, 0, EFI_NULL_MODIFIER, 0},
            {EfiKeyZero, '0', '0', 0, 0, EFI_INSERT_MODIFIER, STD | EFI_AFFECTED_BY_NUM_LOCK},
            {EfiKeyPeriod, '.', '.', 0, 0, EFI_DELETE_MODIFIER, STD | EFI_AFFECTED_BY_NUM_LOCK},
            {EfiKeyOne, '1', '1', 0, 0, EFI_END_MODIFIER, STD | EFI_AFFECTED_BY_NUM_LOCK},
            {EfiKeyTwo, '2', '2', 0, 0, EFI_DOWN_ARROW_MODIFIER, STD | EFI_AFFECTED_BY_NUM_LOCK},
            {EfiKeyThree, '3', '3', 0, 0, EFI_PAGE_DOWN_MODIFIER, STD | EFI_AFFECTED_BY_NUM_LOCK},
            {EfiKeyFour, '4', '4', 0, 0, EFI_LEFT_ARROW_MODIFIER, STD | EFI_AFFECTED_BY_NUM_LOCK},
            {EfiKeyFive, '5', '5', 0, 0, EFI_NULL_MODIFIER, STD | EFI_AFFECTED_BY_NUM_LOCK},
            {EfiKeySix, '6', '6', 0, 0, EFI_RIGHT_ARROW_MODIFIER, STD | EFI_AFFECTED_BY_NUM_LOCK},
            {EfiKeySeven, '7', '7', 0, 0, EFI_HOME_MODIFIER, STD | EFI_AFFECTED_BY_NUM_LOCK},
            {EfiKeyEight, '8', '8', 0, 0, EFI_UP_ARROW_MODIFIER, STD | EFI_AFFECTED_BY_NUM_LOCK},
            {EfiKeyNine, '9', '9', 0, 0, EFI_PAGE_UP_MODIFIER, STD | EFI_AFFECTED_BY_NUM_LOCK},
            {EfiKeyLCtrl, 0, 0, 0, 0, EFI_LEFT_CONTROL_MODIFIER, 0},
            {EfiKeyRCtrl, 0, 0, 0, 0, EFI_RIGHT_CONTROL_MODIFIER, 0},
            {EfiKeyLShift, 0, 0, 0, 0, EFI_LEFT_SHIFT_MODIFIER, 0},
            {EfiKeyRShift, 0, 0, 0, 0, EFI_RIGHT_SHIFT_MODIFIER, 0},
            {EfiKeyLAlt, 0, 0, 0, 0, EFI_LEFT_ALT_MODIFIER, 0},
            {EfiKeyA2, 0, 0, 0, 0, EFI_RIGHT_ALT_MODIFIER, 0},
            {EfiKeyA0, 0, 0, 0, 0, EFI_NULL_MODIFIER, 0},
            {EfiKeyA3, 0, 0, 0, 0, EFI_NULL_MODIFIER, 0},
        },
        {'H', 'o', 'm', 'e', 'l', 'a', 'b', ' ', 'F', 'R', ' ', 'A',
         'Z', 'E', 'R', 'T', 'Y', ' ', 'k', 'e', 'y', 't', 'e', 's', 't'},
    },
    HII_HEADER(4, EFI_HII_PACKAGE_END),
};

#ifndef KEYTEST_DRIVER_ONLY
static void print(CHAR16 *s) {
  gST->ConOut->OutputString(gST->ConOut, s);
}

static void print_hex16(UINT16 v) {
  CHAR16 buf[7];
  static CHAR16 hex[] = {'0', '1', '2', '3', '4', '5', '6', '7',
                         '8', '9', 'A', 'B', 'C', 'D', 'E', 'F'};
  buf[0] = '0';
  buf[1] = 'x';
  buf[2] = hex[(v >> 12) & 0xf];
  buf[3] = hex[(v >> 8) & 0xf];
  buf[4] = hex[(v >> 4) & 0xf];
  buf[5] = hex[v & 0xf];
  buf[6] = 0;
  print(buf);
}

static void print_hex64(UINT64 v) {
  CHAR16 buf[19];
  static CHAR16 hex[] = {'0', '1', '2', '3', '4', '5', '6', '7',
                         '8', '9', 'A', 'B', 'C', 'D', 'E', 'F'};
  buf[0] = '0';
  buf[1] = 'x';
  for (UINTN i = 0; i < 16; i++) {
    buf[2 + i] = hex[(v >> ((15 - i) * 4)) & 0xf];
  }
  buf[18] = 0;
  print(buf);
}

static void dump_keys_until(UINT16 stop_scan, CHAR16 stop_char) {
  for (;;) {
    EFI_INPUT_KEY key;
    UINTN index;
    gBS->WaitForEvent(1, &gST->ConIn->WaitForKey, &index);
    if (EFI_ERROR(gST->ConIn->ReadKeyStroke(gST->ConIn, &key))) {
      continue;
    }

    print((CHAR16 *)L"Scan=");
    print_hex16(key.ScanCode);
    print((CHAR16 *)L" Unicode=");
    print_hex16(key.UnicodeChar);
    print((CHAR16 *)L" Char=[");
    if (key.UnicodeChar >= 0x20 && key.UnicodeChar != 0x7f) {
      CHAR16 c[2] = {key.UnicodeChar, 0};
      print(c);
    }
    print((CHAR16 *)L"]\r\n");

    if ((stop_scan && key.ScanCode == stop_scan) || (stop_char && key.UnicodeChar == stop_char)) {
      return;
    }
  }
}

EFI_STATUS EFIAPI efi_main(EFI_HANDLE ImageHandle, EFI_SYSTEM_TABLE *SystemTable) {
  EFI_HII_DATABASE_PROTOCOL *hii = NULL;
  EFI_HII_HANDLE hii_handle = NULL;
  EFI_STATUS status;

  gST = SystemTable;
  gBS = SystemTable->BootServices;
  gST->ConIn->Reset(gST->ConIn, 0);

  print((CHAR16 *)L"\r\nHomelab rEFInd AZERTY HII keytest\r\n");
  print((CHAR16 *)L"BEFORE SetKeyboardLayout: press physical AZERTY A/Q/Z/W and 2/e-acute.\r\n");
  print((CHAR16 *)L"Press Enter to register and activate the test HII layout.\r\n\r\n");
  dump_keys_until(0, 0x000d);

  status = gBS->LocateProtocol(&gEfiHiiDatabaseProtocolGuid, NULL, (VOID **)&hii);
  print((CHAR16 *)L"\r\nLocateProtocol(EFI_HII_DATABASE_PROTOCOL): ");
  print_hex64(status);
  print((CHAR16 *)L"\r\n");
  if (EFI_ERROR(status)) {
    print((CHAR16 *)L"No HII database protocol. HII keymap approach cannot work here.\r\n");
    print((CHAR16 *)L"Press Esc to exit.\r\n");
    dump_keys_until(0, 0x001b);
    return status;
  }

  status = hii->NewPackageList(hii, &gAzertyPackage.Header, ImageHandle, &hii_handle);
  print((CHAR16 *)L"NewPackageList(AZERTY layout): ");
  print_hex64(status);
  print((CHAR16 *)L"\r\n");
  if (EFI_ERROR(status)) {
    print((CHAR16 *)L"Firmware rejected the keyboard package. Press Esc to exit.\r\n");
    dump_keys_until(0, 0x001b);
    return status;
  }

  status = hii->SetKeyboardLayout(hii, &gHomelabFrAzertyLayoutGuid);
  print((CHAR16 *)L"SetKeyboardLayout(AZERTY): ");
  print_hex64(status);
  print((CHAR16 *)L"\r\n\r\n");
  if (EFI_ERROR(status)) {
    print((CHAR16 *)L"Firmware did not accept the layout GUID. Press Esc to exit.\r\n");
    dump_keys_until(0, 0x001b);
    return status;
  }

  print((CHAR16 *)L"AFTER SetKeyboardLayout: press the same physical keys.\r\n");
  print((CHAR16 *)L"Success signal: physical AZERTY A should now print Unicode 0x0061, Q -> 0x0071,\r\n");
  print((CHAR16 *)L"Z -> 0x007A, W -> 0x0077, and unshifted 2/e-acute -> 0x00E9.\r\n");
  print((CHAR16 *)L"If the values are unchanged from BEFORE, this firmware ignores runtime HII layouts.\r\n");
  print((CHAR16 *)L"Press Esc to exit.\r\n\r\n");
  dump_keys_until(0, 0x001b);
  return EFI_SUCCESS;
}
#else
EFI_STATUS EFIAPI efi_main(EFI_HANDLE ImageHandle, EFI_SYSTEM_TABLE *SystemTable) {
  EFI_HII_DATABASE_PROTOCOL *hii = NULL;
  EFI_HII_HANDLE hii_handle = NULL;
  EFI_STATUS status;

  gST = SystemTable;
  gBS = SystemTable->BootServices;

  status = gBS->LocateProtocol(&gEfiHiiDatabaseProtocolGuid, NULL, (VOID **)&hii);
  if (EFI_ERROR(status)) {
    return status;
  }

  status = hii->NewPackageList(hii, &gAzertyPackage.Header, ImageHandle, &hii_handle);
  if (EFI_ERROR(status)) {
    return status;
  }

  return hii->SetKeyboardLayout(hii, &gHomelabFrAzertyLayoutGuid);
}
#endif
