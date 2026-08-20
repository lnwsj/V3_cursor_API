# Browser UI observations

Captured with the connected browser against https://green.cutdee.com/ without
uploading a file or entering an API token.

## Home / TC01

- V1.1.1
- h264_nvenc hardware badge
- 401 GB free at capture
- Drop zones: Cover optional, Product required, Background required, Audio optional
- Render disabled with zero files
- Gallery shows Token required / ตั้งค่า API token ก่อนดู output

Screenshot: screenshots/01_home_upload_zones.png
Checkpoint aliases also captured:
screenshots/01_open_page.png
screenshots/02_input_ready.png
screenshots/03_click_generate.png

The click-generate checkpoint was a read-only guard observation: Render was
disabled because no files and no authenticated session were supplied. No POST
render request was triggered.

## TC05

- Product is the source video; Background, Audio and Cover are unused.
- UI describes 7 lens x 3 composition output matrix.
- Render disabled with zero Product files.

Screenshot: screenshots/02_tc05_upload_contract.png

## TC06

- Product: one or more green-screen videos.
- Background: one or more background files.
- Audio: one master audio file used for every output.
- Cover is unused.
- Render disabled until Product + Background + Audio are present.

Screenshot: screenshots/03_tc06_upload_contract.png
Audio checkpoint alias: screenshots/05_audio_ready_or_error.png. It shows the
TC06 audio input contract with zero files; it is not a real audio-ready render.

## Output page

Clicking Open Output opens /outputs/ in a new tab. Without an authenticated
session the page still renders the gallery shell but displays
ตั้งค่า API token ก่อนดู output.

Screenshots:
screenshots/04_output_token_guard.png
screenshots/05_outputs_page_token_guard.png
Result checkpoint alias: screenshots/04_result_state.png. It shows the
authenticated-output guard, not a completed render result.
