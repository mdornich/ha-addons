# ha-addons

Home Assistant add-on repository for the Crepe Myrtle instance.

- `openai_realtime_voice_agent/` — vendored from
  [xandervanerven/ha-openai-realtime](https://github.com/xandervanerven/ha-openai-realtime)
  at `3edc58679a7f59dce1d31e0a7e5784d04c179f79` (v0.6.0, MIT), with one change:
  `image:` points at a prebuilt aarch64 image on ghcr.io so the Pi pulls instead of
  compiling from source. Build recipe and provenance:
  `hey-trixie-wakeword/VENDORED.md`. Tracked in 980labsOS issue #1141.

Add to HA: Settings → Add-ons → Add-on Store → ⋮ → Repositories →
`https://github.com/mdornich/ha-addons`.
