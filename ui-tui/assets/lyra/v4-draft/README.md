# Lyra portrait regression sources

`avatar-32.json`, `avatar-40.json`, and `avatar-48.json` contain the approved
16-color native grids used by the portrait color-preservation tests. Their
background remains ivory so tests can distinguish intended transparency from
changes to the character's colors.

The runtime atlas is [`src/assets/lyra-v4.json`](../../../src/assets/lyra-v4.json).
The tests also retain the original full figure from
[`src/assets/lyra-v3.json`](../../../src/assets/lyra-v3.json).
The runtime selects a native grid that fits the terminal; it never resamples
a portrait. Earlier image-generation drafts and comparison sheets are local
archive material and are excluded from version control.
