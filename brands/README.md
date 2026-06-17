# Brand assets

Logo/icon for the Home Assistant integration tile. HA fetches these from the
[home-assistant/brands](https://github.com/home-assistant/brands) repository by
domain — they are **not** loaded from this repo at runtime.

```
custom_integrations/weber_connect/
  icon.png       256x256   icon@2x.png   512x512
  logo.png       256x256   logo@2x.png   512x512
```

Source: Material Design Icons `grill` glyph (Apache-2.0, no attribution required),
recolored to Material red and rendered with transparency. Source SVG: `../icons/grill.svg`.

To publish: open a PR adding `custom_integrations/weber_connect/` (these files) to
home-assistant/brands. Until then the integration shows HA's default icon.
