# Sample Audio Files

These files are model-quality investigation fixtures. They are not public API upload fixtures; `/v1/transcribe` accepts only HeliBoard's canonical 16 kHz mono 16-bit PCM WAV files with a 44-byte header.

## recording_with_hebrew.wav

38-second Hebrew conversation with English code-switching. Single speaker discussing IPA requests and Xilinx hardware. This file is 48 kHz stereo with an extra WAV chunk and is intentionally rejected by the locked-down HeliBoard upload contract.

## letter_factory_leap_frog.wav

Audio extracted from [Letter Factory | Leap Frog](https://www.youtube.com/watch?v=wTiyM_lvayI) (7 min 7 sec). Children's educational content in English — useful for testing long-form transcription with multiple speakers.
