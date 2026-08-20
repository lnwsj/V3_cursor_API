# Browser UI observations

- URL: `https://green.cutdee.com/`
- The connected browser observed the deployed public UI, not a local frontend checkout.
- `01_open_page.png`: home surface, TC01 selected, UI version `v1.1.1`, `h264_nvenc`, approximately `397 GB free`, zero files, Render disabled.
- `02_input_ready.png`: TC05 selected (`ออโต้ซูมล้วน`); Product is required, Background/Audio/Cover are optional for this mode; zero files.
- `03_click_generate.png`: Render click was attempted through the visible button; the UI prevented the action because the button was disabled while no files were present. No job was created.
- `04_result_state.png`: History/output surface showed `Token required` and `Error: 401 authentication required`; no token was entered or stored.
- `05_audio_ready_or_error.png`: TC06 selected (`Chroma Audio Master`); Product, Background, and Audio controls are visible; no files were uploaded.
- The live page JavaScript references `/api/render/<tc>`, `/api/job/<id>`, `/api/job/<id>/output`, `/api/job/<id>/download-all`, `/api/job/<id>/thumbnails`, `/api/outputs`, `/api/download/<path>`, `/api/jobs/history`, and `/api/jobs/list`.
- No real media upload, render, authenticated history read, or output download was performed in this study.

## Screenshot evidence

The five images are actual browser captures and were converted to valid PNG files. SHA-256 values are in `logs/artifact_hashes.log`.
