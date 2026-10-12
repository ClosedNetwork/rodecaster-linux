# RØDECaster Pro (gen 1) companion protocol

Reconstructed from a static analysis of **RODECaster Pro.app 2.1.2** (macOS, x86_64, bundle
`com.rode.rodecasterpro`, JUCE). I disassembled it with Ghidra 11.3.2 headless. Function
addresses below refer to that binary. **Nothing here has been checked against a live unit yet.**
Each item is marked as one of:

- **observed:** read directly from the app's code.
- **inferred:** a reasonable reading of that code, still unconfirmed.
- **unknown:** not worked out yet.

## Transport (observed)

- USB vendor `0x19F7`. The app matches product IDs `0x0011`, `0x0012` and `0x0019`
  (`FUN_100003f90`, `FUN_100004140`). `0x0019` is the unit's USB transfer mode (USB product string
  "RODECaster Pro USB Transfer Mode"; the microSD card is exposed as mass storage).
- All control goes through **one HID interface** using numbered reports. The app only ever
  sends with `IOHIDDeviceSetReport(kIOHIDReportTypeOutput, …)`. Its input callback strips the
  report-ID byte and queues the report.

| Dir | ID | Body size | Use |
|-----|----|-----------|-----|
| OUT | 2 | 9    | simple command `[cmd, 0…]` |
| IN  | 1 | 9    | simple reply `[cmd, 'A'/'E'/'N', v_lo, v_hi, u32 value]` |
| OUT | 5 | 64   | command header **or** command payload |
| IN  | 4 | 64   | header ack **or** response payload |
| OUT | 3 | 2048 | bulk host→device (audio, settings) |
| IN  | 6 | 2048 | bulk device→host (audio, settings) |

On Linux this maps directly onto `/dev/hidrawN`: the first byte of every `write()` and `read()`
is the report ID.

## Simple commands (report 2 → 1)

> **Checked on a live unit (2026-10-12, normal mode `0x0011` and transfer mode `0x0019`):** the
> unit accepts report-2 writes but never answers `V` or `I` on report 1, with or without the
> report-ID byte in the payload. GET_REPORT on reports 1 and 4 stalls. The report-5/4 command
> channel *does* work in normal mode: status (`0x18`), free memory (`0x08`), bank count, bank
> names and pad reads all return sensible data. Reading status clears bit1. In transfer mode
> the simple `V`/`I` queries also go unanswered, but the command channel still works: the
> group-1 "leave transfer mode" action switches the unit back to normal mode (confirmed; the
> unit re-enumerates as `0x0011` about 3 s later). "Enter transfer mode" from normal mode is
> confirmed too.

| Cmd | Meaning | Reply |
|-----|---------|-------|
| `V` (0x56) | firmware version | `'A'`, version = `b3 . (b2>>4) . (b2&15)` (observed; `'E'` means unavailable) |
| `I` (0x49) | identify | `'A'`, version as above, plus u32 at bytes 4–7 (observed; meaning unknown) |
| `R` (0x52) | reset/restart | write only; the app doesn't wait for a reply (observed) |
| `E`, `P` | erase page / program | bootloader only, used by the firmware updater (observed; **not implemented**) |

## Command channel (report 5 / 4), `FUN_10028eb10`

The header is 64 bytes (observed):

```
00  52 0E B0 3E        magic
04  dir                0 = write, 1 = read
05  group              3 = data, 1 = system action
06  u16 index          1 for data commands; the action number for group 1
08  sel[4]             [cmd, arg1, arg2, arg3]
0C  zero padding to 64
```

- **Read:** send the header, then wait for report 4. That report is the response.
- **Write:** send the header, wait for the report-4 ack, send the 64-byte payload on report 5,
  then wait for report 4.
- The app retries each transaction 3 times and waits up to 5 s for each report.

### Group 3 commands

A pad is addressed as `(bank << 3) | pad`, giving 8 banks × 8 pads.

| cmd | R/W | args | payload | notes |
|----|-----|------|---------|-------|
| 0x00 | RW | pad | `[R,G,B]` | pad colour (observed) |
| 0x01 | RW | pad | `[mode]` | playback style. Modes are listed in the order the app shows them: Latch, Pause, Replay, Play, `!@#*`, Trash-talk = 0…5 (**inferred** from UI order) |
| 0x02 | RW | pad | `char[16]` | pad name (observed) |
| 0x03 | W | pad, channels | `u32 frames, char[12] name` | starts an upload. Sending `frames=0` clears the pad (observed) |
| 0x07 | R | pad | `u32` | bytes of audio stored on the pad (observed) |
| 0x08 | R | – | `u32` | free sound memory in bytes (observed) |
| 0x09 | R | pad | `u32` | number of bytes a download will return (observed) |
| 0x0C | R | – | `[n]` | banks enabled − 1 (observed: the app clamps it to 7 and adds 1) |
| 0x0D | R | pad | `u32` | checksum of the pad audio, used as a cache key (observed) |
| 0x0E | R | – | – | starts a download; report-6 chunks follow (observed) |
| 0x0F | R | pad | `[channels, flag]` | byte 0 is written into the WAV header as channels, and the app stops if it is 0. Byte 1 decides an optional local "Finalising Audio" pass (observed) |
| 0x10 | RW | bank | `char[16]` | bank name (observed) |
| 0x12 | W | bank | – | clears the bank. "Restore factory sounds" sends it for banks 7…0 (observed) |
| 0x13 | R | – | `u32` | settings size. The app requires `0x400` (observed) |
| 0x14 | RW | – | – | settings backup (read, then one report-6 bulk read) and restore (write with a zero payload, then one report-3 bulk write and a report-4 ack) (observed) |
| 0x15 | W | – | `h,m,s,day,month,u16 year` | sets the clock (observed) |
| 0x16 | W | – | – | queued when the user cancels a pad download (observed) |
| 0x17 | RW | channel | 48-byte processing block | channels 0–3 are mics; app slot 4 is sent as wire channel 8 (observed) |
| 0x18 | R | – | `[flags]` | status. bit0 is effects-edit mode (inferred). bit1 means "re-read everything" (observed). bit2 is unknown |
| 0x19 | RW | pad | `[0/1]` | loop (observed) |
| 0x1B | W | – | magic | enters the bootloader before a firmware update (observed; **never sent**) |

### Group 1 actions (`index` field)

| index | action |
|------|--------|
| 5 | enter Podcast Transfer Mode (observed: tied to the app's confirmation dialog) |
| 6 | leave Podcast Transfer Mode (observed) |
| 7 | effects edit mode, with the selector as a u32 0/1 (**inferred** from the effects editor's toggle) |

## Audio transfer (`FUN_1001c0fe0` upload, `FUN_1001c2720` download)

**Upload** (observed):

1. Decode the file and resample it to 48 kHz. Keep the source's channel count, capped at 2.
2. Read free memory (0x08) and the target pad's current size (0x07). The app shows "Not enough
   memory" if the new audio needs more than their sum.
3. Send 0x03 with the frame count and the first 12 characters of the filename.
4. Stream interleaved s16le audio in 2048-byte report-3 chunks (1024 mono frames or 512 stereo
   frames each). Zero-pad the last chunk and wait for report 4 after every chunk.
5. Read 0x0D (checksum) and 0x07 (size).

**Download** (observed):

1. Read 0x07 (size), 0x0F (format) and 0x09 (length). If any of them is 0, stop.
2. Send 0x0E with selector `[0E 00 00 00]`, then read ⌈len/2048⌉ report-6 chunks.
3. The app writes these as 16-bit 48 kHz PCM WAV. If 0x0F byte 1 is set, it also runs a local
   normalising pass ("Finalising Audio"). If the user cancels, 0x16 is sent.

## Effects block (0x17), 48 bytes

Field order and sizes come from the app's reset-to-defaults routine (`FUN_100122900`), checked
against the 36-entry key table in the preset code (`FUN_100123270`). Byte offsets and defaults:

```
0 EqCutoff u8 60          1 GateThreshold i16 -522   3 GateAttack i16 1585
5 GateRelease i16 70       7 GateDecay i16 160        9 GateRange i16 137
11 GateHysteresis u8 0     12 DeesserThreshold i16 -390 14 DeesserRatio u8 24
15 DeesserAttack u8 16     16 DeesserRelease i16 50   18 DeesserGain u8 0
19 DeesserFrequency i16 5856 21 CompThreshold i16 -192 23 CompRatio u8 20
24 CompAttack u8 2         25 CompRelease i16 220     27 CompGain u8 45
28 CompKnee u8 0           29 ExciterTune i16 3631    31 ExciterHarmonics u8 60
32 ExciterMix u8 50        33 BBDrive u8 60           34 BBTune u8 100   35 BBMix u8 30
36..42 enables: Dynamics, HPFilter, NoiseGate, Deesser, AuralExciter, BigBottom, AllProcessing
43..46 ReverbSendLevel, ReverbModel, ReverbDecay, ReverbCutoff (u8, inferred)   47 trailer
```

Units and scaling are **unknown**. For example, −522 may mean −52.2 dB, but that is unconfirmed.

## Files

- `.rcpsettings` = `"RCP_SET[v1.0.0]\0"` followed by 0x400 bytes of settings (observed).
- Factory sounds are 8 MP3s embedded in the app. Their offsets are in `rodecaster/assets.py`
  (observed).
- Recordings live under `/RODE/PODCASTS` on the card. The app scans every mounted volume for
  that folder and offers stereo/multitrack (14-channel) export with per-platform loudness
  presets.

## Hardware validation checklist

Run these with `-vv` so every HID report is logged as hex:

1. `rodecaster-cli list`: should show `19f7:0011` with `command-interface`.
2. `rodecaster-cli -vv info`: firmware should read 2.1.2 (or whatever your unit has).
3. `rodecaster-cli pads --bank 1`: names and colours should match the hardware.
4. `rodecaster-cli set-pad 8 8 --colour #00ff00`: the bank-8 pad-8 LED should turn green.
5. `rodecaster-cli download 1 1 /tmp/p.wav`: check that it plays correctly. If it is noise, the
   0x0F channel byte is wrong.
6. `rodecaster-cli upload 8 8 some.wav`, then press bank 8 / pad 8 on the unit.

Please record the results in an issue, especially for any row marked inferred.
