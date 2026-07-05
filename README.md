# CWAtlas Collector + MCP Sidecar

Off-device autonomous CW **search→capture** collector for the Web-888 SDR, plus an **MCP
control plane** so an LLM agent can observe and steer it. Feeds raw narrowband IQ to the
**MorseBase** training corpus.

See [DESIGN.md](DESIGN.md) for the full architecture. **This is a skeleton** — the
structure, data model, scheduler policy, and MCP tool surface are real; the WS frame
decode, CW detector, capture writer, and catalog DB are stubbed (`TODO[...]`) pending the
hardware (~June 2026).

## Layout

```
collector/
  DESIGN.md                # architecture + roadmap (read this first)
  pyproject.toml
  cwatlas_mcp/
    models.py              # Detection / ChannelState / TxEvent / Nudge dataclasses
    sdr_client.py          # async AJAX + WebSocket client for the Web-888
    scheduler.py           # Activity Map + Supervisor (the LLM-free brain) + ControlBus
    server.py              # MCP sidecar: observe / nudge / TX tool families
    runtime.py             # wires supervisor + search/PTT workers + MCP together
```

## Quickstart (dev, no hardware)

```bash
cd ~/cwatlas/collector
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Inspect the MCP tool surface (no collector attached):
python -m cwatlas_mcp.server      # stdio; connect an MCP client/inspector

# Full app (supervisor + search/PTT workers + MCP); needs a reachable SDR for real data:
python -m cwatlas_mcp.runtime
```

Point it at your device by editing `SdrConfig` (host/port/password) in `runtime.py`, or
wire it to env/config at M0.

## Auditioning captures

Captures are SigMF: headerless complex int16 IQ at 1.5 kHz (`ci16_le`, carrier at
~+250 Hz baseband) — they play as noise in a media player. Use
`scripts/sigmf_listen.py` to render them as WAV at an audible sidetone pitch, and to
pick strong captures from the catalog:

```bash
.venv/bin/python scripts/sigmf_listen.py --top 20 --band 40m   # list strongest clean captures
.venv/bin/python scripts/sigmf_listen.py --id 1902             # write <capture>.wav beside the data
```

Details in [docs/sigmf_listen.md](docs/sigmf_listen.md).

## Design invariants (don't break these)

- **MCP is control plane only** — never stream IQ/audio through tool calls.
- **The supervisor is authoritative** — agent tools enqueue *nudges*, they don't drive
  channels directly. Collection must survive the agent/MCP being down (Model B).
- **TX front-end protection is hardware** (Flex amp-key → sequenced coax relay/limiter).
  `notify_tx` / `ground_antenna` here are data-hygiene and secondary controls, not the
  interlock.
- **Capture raw IQ** (`mod=iq`, `compression=0`); on-device CW decoders are weak-label
  helpers only, kept out of the capture path.

## Milestones

M0 read-only proof · M1 single capture · M2 full 13-ch supervisor · M3 TX hygiene ·
M4 agent nudges · M5 storage lifecycle. (Details in DESIGN.md §12.)
