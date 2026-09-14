import assert from "node:assert/strict";
import {
  formatImageInputDisplay,
  normalizeImageInputValue,
  parseImageInput,
  resolveImageInputSubmitText,
  shouldNormalizeImageInputValue,
  updateImageInputValue,
  normalizeMultilineInputValue,
} from "./image-command.js";

assert.deepEqual(
  parseImageInput('/image "D:\\桌面\\Agent_Lab\\agent-system\\f4e6e7dbe2337184.png" 这张图是什么？'),
  {
    path: "D:\\桌面\\Agent_Lab\\agent-system\\f4e6e7dbe2337184.png",
    prompt: "这张图是什么？",
  },
);

assert.deepEqual(
  parseImageInput('"D:\\桌面\\Agent_Lab\\agent-system\\f4e6e7dbe2337184.png"'),
  {
    path: "D:\\桌面\\Agent_Lab\\agent-system\\f4e6e7dbe2337184.png",
    prompt: "Describe this image.",
  },
);

assert.deepEqual(
  parseImageInput('"D:\\桌面\\Agent_Lab\\agent-system\\f4e6e7dbe2337184.png" 分析这张图'),
  {
    path: "D:\\桌面\\Agent_Lab\\agent-system\\f4e6e7dbe2337184.png",
    prompt: "分析这张图",
  },
);

assert.deepEqual(parseImageInput("hello.png is a file name in text"), null);
assert.deepEqual(parseImageInput("/image"), null);

assert.equal(
  formatImageInputDisplay(
    "D:\\桌面\\SillyTavern-fresh\\角色卡\\V2_12.pngD:\\桌面\\SillyTavern-fresh\\角色卡\\main_reitia-overwritten-rabbit-30a97d6be1ef_spec_v2.png",
  ),
  "[Image #1] [Image #2]",
);

assert.equal(
  formatImageInputDisplay(
    "/image D:\\桌面\\SillyTavern-fresh\\角色卡\\V2_12.pngD:\\桌面\\SillyTavern-fresh\\角色卡\\main_reitia-overwritten-rabbit-30a97d6be1ef_spec_v2.png 分析这两张图",
  ),
  "分析这两张图\n[Image #1] [Image #2]",
);

assert.equal(formatImageInputDisplay("hello.png is a file name in text"), null);

const normalized = normalizeImageInputValue(
  "D:\\cards\\V2_12.pngD:\\cards\\main_reitia-overwritten-rabbit-30a97d6be1ef_spec_v2.png",
);
assert.deepEqual(normalized, {
  displayText: "[Image #1] [Image #2]",
  attachments: [
    { label: "[Image #1]", path: "D:\\cards\\V2_12.png" },
    { label: "[Image #2]", path: "D:\\cards\\main_reitia-overwritten-rabbit-30a97d6be1ef_spec_v2.png" },
  ],
});

assert.equal(
  resolveImageInputSubmitText("[Image #1] [Image #2] 分析这两张图", normalized?.attachments ?? []),
  "D:\\cards\\V2_12.png D:\\cards\\main_reitia-overwritten-rabbit-30a97d6be1ef_spec_v2.png 分析这两张图",
);

const appended = updateImageInputValue("[Image #1] D:\\cards\\second.png", [
  { label: "[Image #1]", path: "D:\\cards\\first.png" },
]);
assert.deepEqual(appended, {
  displayText: "[Image #1] [Image #2]",
  attachments: [
    { label: "[Image #1]", path: "D:\\cards\\first.png" },
    { label: "[Image #2]", path: "D:\\cards\\second.png" },
  ],
});

assert.equal(
  resolveImageInputSubmitText(`${appended.displayText} 逐个分析`, appended.attachments),
  "D:\\cards\\first.png D:\\cards\\second.png 逐个分析",
);

assert.deepEqual(
  updateImageInputValue("[Image #1] [Image #2", appended.attachments),
  {
    displayText: "[Image #1]",
    attachments: [{ label: "[Image #1]", path: "D:\\cards\\first.png" }],
  },
);

assert.equal(
  shouldNormalizeImageInputValue("[Image #1] 读取", [{ label: "[Image #1]", path: "D:\\cards\\first.png" }]),
  false,
);

assert.equal(
  shouldNormalizeImageInputValue("[Image #1] D:\\cards\\second.png", [
    { label: "[Image #1]", path: "D:\\cards\\first.png" },
  ]),
  true,
);

assert.equal(
  shouldNormalizeImageInputValue("[Image #1] [Image #2", appended.attachments),
  true,
);

assert.equal(
  normalizeMultilineInputValue("第一行\r\n第二行\n\n第三行"),
  "第一行 / 第二行 / 第三行",
);
