# ClearFrame local studio

Local derivative of CXP-2024/gpt-anime-denoise at a5005a82f70d1d7c79145968c7c64906b283eb18. Preserve upstream attribution. Never install an automatic image-processing skill. No cloud inference or OpenAI credentials used. Images are imported and processed only after an explicit Start click.

## Ownership
- root: studio/server.py, studio/hardware.py, launch/setup, integration and runtime validation.
- engine agent: studio/engine.py, studio/presets.py, engine tests.
- frontend agent: web/index.html, web/app.js, web/style.css.

## Engine contract
`studio.presets.get_config()` returns JSON-serializable {modes,methods,stages,defaults}. Modes: anime_character, anime_landscape, photo_portrait, photo_landscape. A mode item has id,label,description,default_method (default),default_strength (0..100),recommendation,risks. Methods have id,label,principle,effect,caution. Required IDs: default, gentle, texture_clean, custom. Stages IDs: photo_denoise, anime_sr, l0, sharpen. Stage items have id,label,principle,effect,caution. Defaults includes mode,method,strength,device,stages. No claim that one recipe fits all photos.

`studio.engine.process_image(source_path, output_path, settings, progress, cancelled)` returns JSON-serializable metadata including actual_device, applied_stages, duration_seconds. Settings: mode, method, strength 0..100, device auto/cpu/cuda, stages (array of stage IDs, custom only). `progress(percent,stage,detail=None)` reports real stage/iteration boundaries with monotonic percentage. `cancelled()` bool. Raise `ProcessingCancelled` on cancel, `ValueError` invalid inputs. Source never overwritten. Model dir is project/.models via CLEARFRAME_MODELS environment variable. Use upstream denoise core for anime processing, cautious OpenCV NLM for photo processing, optional restrained sharpen. Preserve source size and alpha; keep RGB ICC where possible. No torch import at app startup if avoidable; import lazily for processing. Photo pipeline can run on CPU without torch. Strength zero produces exact pixel identity.

## HTTP contract
- GET /api/config -> config above plus {app_name,version,output_dir}.
- POST /api/images?name=<filename>, body raw image bytes -> {id,name,width,height,source_url,has_alpha}. Import NEVER starts processing.
- POST /api/jobs JSON {image_id,mode,method,strength,device,stages} -> {id,image_id,status:"queued"}.
- GET /api/jobs/<id> -> {id,image_id,status,progress,stage,detail,elapsed_seconds,actual_device,result_url,download_url,error,settings}. Status queued/running/completed/failed/cancelled. null URLs until completed.
- POST /api/jobs/<id>/cancel -> {ok:true}.
- GET /api/images/<id>/source -> normalized PNG.
- GET /api/jobs/<id>/result[?download=1] -> PNG.
- GET /api/hardware -> {cpu_percent,ram_used_gb,ram_total_gb,gpu:{name,utilization,memory_used_gb,memory_total_gb,temperature_c}|null,cuda_available,active_device,active_job_id}. Missing measurements null, never fictional.
- GET /api/health -> {ok:true,app:"clearframe",version}.

Mutating requests require X-Token header from `window.CLEARFRAME_TOKEN` injected by server into index.html's `__TOKEN__`. Same-origin loopback only. Server startup command `python -m studio.server --port 7866 --no-browser`, defaults to browser open. One worker, async job polling every ~500ms, hardware every ~2s. Paths project/.runtime (imports), project/results (persistent outputs). Unique output names. Model downloads are first-use and explicitly shown in processing stage.

## UI acceptance
Chinese polished desktop image workspace, drag/drop/multiple selection queue, manual Start only; 4 scenario cards, processing method selection with principles/effects/cautions, custom stage checkboxes and strength slider with label; auto/GPU/CPU device choice, real progress/cancel/errors, live hardware panel; original/result/split-comparison modes, zoom/fit, slider, download. Preserve each file's last successful result when settings change, label old result clearly. Avoid presenting percentages as measured quality. No fabricated preview/demo output.

## Validation
Meaningful unit tests for mapping, identity, alpha/size, cancellation and invalid inputs; HTTP lifecycle/auth checks; browser interaction and real GPU anime processing plus real CPU photo processing. Preserve upstream core. Install dependencies in project-local .venv. Keep intermediate files in work/. Commit local branch after validation. Remote GitHub fork/push only if user connection is available.
