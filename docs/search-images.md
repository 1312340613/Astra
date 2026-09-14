# Search responsiveness and image galleries

Astra keeps the configured search provider and search type. `search_web` and
`web_extract` explicitly opt into bounded parallel scheduling without changing
their `network` risk. Individual results are sent to the frontend on completion;
model tool messages remain paired and ordered by the original call IDs. Other
network actions retain ordered execution unless explicitly reviewed and opted in.

## Find images

Ask Astra, for example: `帮我找几张日式庭院的参考图`.

`search_images(query, max_results=6)` uses the existing `EXA_API_KEY` (or
`EXA_ENV_FILE`) and `EXA_API_URL`. No additional service key is required. It finds
images from matching webpages using Exa image links and representative images.
It is not reverse-image search or visual similarity search.

The current model can visually inspect candidates with
`read_search_images(image_ids, question)`, using IDs returned by `search_images`.
For example: `帮我找几张日式庭院参考图，实际看图后选出有池塘和石灯笼的图片`.
Search results start as `not_inspected`. The reader loads one to four images
concurrently and sends actual pixels, image IDs and source labels through
Astra's existing image-attachment and vision-preprocessing flow. The model then
checks the scene and reports its findings. A successful download only means
`awaiting_model_inspection`; it does not certify that the image matches the
query. No additional API key or model provider is required. Visual inspection
uses the current model and its normal image-token budget.

Downloaded images are limited to 8 MiB and 16 megapixels, with bounded redirects,
timeouts and public-URL checks at each hop. The actual raster is decoded before
attachment; AVIF is converted to PNG without resizing, while other supported
formats retain their bytes. Files are stored under the tool artifact directory's
`search-images` folder and reused while present. Failures are reported separately
by image ID, so other selected images can still be inspected. The ID lookup is
bounded and process-local; rerun search after restarting Astra or if an ID has
expired. A text-only model must report that it cannot inspect pixels unless an
existing external vision tool is available.

The tool returns at most 10 images with stable IDs, source-page links and image
links, plus an HTML gallery under the configured tool artifact directory's
`image-galleries` subdirectory. The TUI shows a compact result and a command:

- `/gallery` opens the latest available image-search gallery in the current TUI.
- `/gallery 4` opens the gallery attached to tool result #4.
- `/tool 4` shows that result's details.

These local commands remain available while another tool is working. Restart
Astra after updating the Python runtime and building `ui-tui` to load the new
runtime, command menu and UI bundle. Galleries are standalone local files; no
persistent web server is needed for normal use.

## Result quality and limitations

Candidates are URL-deduplicated, distributed across source pages, and filtered
for obvious interface assets (SVGs, avatars, logos and login graphics). Public
URL checks and bounded HEAD requests reject inaccessible or tiny image files;
redirect targets are checked before following them. A valid HEAD response is
not a guarantee that a browser will display the image later: hosts can change
access rules or disallow embedding. Fewer results can be returned when candidates
fail checks. Original image resolution and licensing are not inferred.

The gallery escapes external text, uses a restrictive content security policy,
loads previews lazily with no referrer, and contains no scripts. It loads images
from their source hosts when opened. Search alone does not add image bytes to the
model context; `read_search_images` explicitly loads the selected candidates for
visual inspection. The model must base any claim about verified visual content
on those pixels. The gallery remains a candidate list, and does not automatically
label all pictures as approved after the model inspects a subset.

Exa source: https://exa.ai/docs/reference/contents-retrieval

## Verification

Search/image tests cover candidate filtering, bounded downloads, image attachment
and gallery commands. A real-model check must inspect the returned pixels before
describing visual content. A successful download or gallery render alone does
not verify relevance. Keep timings and selected-image observations with each
run; model and network variation are separate from tool scheduling overhead.
