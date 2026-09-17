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
  parseImageInput('/image "D:\\测试资料\\图片\\sample.png" 这张图是什么？'),
  {
    path: "D:\\测试资料\\图片\\sample.png",
    prompt: "这张图是什么？",
  },
);

assert.deepEqual(
  parseImageInput('"D:\\测试资料\\图片\\sample.png"'),
  {
    path: "D:\\测试资料\\图片\\sample.png",
    prompt: "Describe this image.",
  },
);

assert.deepEqual(
  parseImageInput('"D:\\测试资料\\图片\\sample.png" 分析这张图'),
  {
    path: "D:\\测试资料\\图片\\sample.png",
    prompt: "分析这张图",
  },
);

assert.deepEqual(parseImageInput("hello.png is a file name in text"), null);
assert.deepEqual(parseImageInput("/image"), null);

assert.equal(
  formatImageInputDisplay(
    "D:\\测试资料\\图片\\first.pngD:\\测试资料\\图片\\second.png",
  ),
  "[Image #1] [Image #2]",
);

assert.equal(
  formatImageInputDisplay(
    "/image D:\\测试资料\\图片\\first.pngD:\\测试资料\\图片\\second.png 分析这两张图",
  ),
  "分析这两张图\n[Image #1] [Image #2]",
);

assert.equal(formatImageInputDisplay("hello.png is a file name in text"), null);

const normalized = normalizeImageInputValue(
  "D:\\images\\first.pngD:\\images\\second.png",
);
assert.deepEqual(normalized, {
  displayText: "[Image #1] [Image #2]",
  attachments: [
    { label: "[Image #1]", path: "D:\\images\\first.png" },
    { label: "[Image #2]", path: "D:\\images\\second.png" },
  ],
});

assert.equal(
  resolveImageInputSubmitText("[Image #1] [Image #2] 分析这两张图", normalized?.attachments ?? []),
  "D:\\images\\first.png D:\\images\\second.png 分析这两张图",
);

const appended = updateImageInputValue("[Image #1] D:\\images\\second.png", [
  { label: "[Image #1]", path: "D:\\images\\first.png" },
]);
assert.deepEqual(appended, {
  displayText: "[Image #1] [Image #2]",
  attachments: [
    { label: "[Image #1]", path: "D:\\images\\first.png" },
    { label: "[Image #2]", path: "D:\\images\\second.png" },
  ],
});

assert.equal(
  resolveImageInputSubmitText(`${appended.displayText} 逐个分析`, appended.attachments),
  "D:\\images\\first.png D:\\images\\second.png 逐个分析",
);

assert.deepEqual(
  updateImageInputValue("[Image #1] [Image #2", appended.attachments),
  {
    displayText: "[Image #1]",
    attachments: [{ label: "[Image #1]", path: "D:\\images\\first.png" }],
  },
);

assert.equal(
  shouldNormalizeImageInputValue("[Image #1] 读取", [{ label: "[Image #1]", path: "D:\\images\\first.png" }]),
  false,
);

assert.equal(
  shouldNormalizeImageInputValue("[Image #1] D:\\images\\second.png", [
    { label: "[Image #1]", path: "D:\\images\\first.png" },
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
