import assert from "node:assert/strict";
import { galleryOpenCommand, imageSearchSummary, openImageGallery, parseImageSearch } from "./image-gallery.js";
import { toolResultBody } from "./tool-results.js";
const path = "/tmp/image-galleries/images-0123456789abcdef0123456789abcdef.html";
const result = { id: 4, name: "search_images", error: "", output: JSON.stringify({
  type: "image_search", success: true, gallery_path: path,
  images: [{ id: "img_a", title: "Garden\u001b[2J", source_url: "https://source.example/", image_url: "https://img.example/a.jpg" }],
}) };
assert.equal(parseImageSearch(result)?.images.length, 1);
assert.match(imageSearchSummary(result)!, /\/gallery 4/);
assert.ok(!imageSearchSummary(result)!.includes("\u001b"));
assert.equal(toolResultBody(result), imageSearchSummary(result));
assert.equal(parseImageSearch({ ...result, output: "truncated{" }), null);
assert.equal(parseImageSearch({ ...result, name: "shell" }), null);
assert.deepEqual(galleryOpenCommand(path, "darwin"), ["open", [path]]);
assert.deepEqual(galleryOpenCommand(path, "linux"), ["xdg-open", [path]]);
assert.throws(() => galleryOpenCommand("https://example.com/a.html"));
assert.throws(() => galleryOpenCommand("/tmp/evil.html"));
assert.throws(() => galleryOpenCommand(path + "\n"));
assert.match(await openImageGallery([], "4"), /没有/);
assert.match(await openImageGallery([], "oops"), /用法/);
console.log("image-gallery tests passed");
