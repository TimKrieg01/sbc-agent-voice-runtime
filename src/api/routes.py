import json
import logging
import asyncio
import re
from pathlib import Path
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import Response, HTMLResponse, JSONResponse
from src.services.connection.orchestrator import ConnectionOrchestrator
from src.core.tenant import get_tenant_config

logger = logging.getLogger(__name__)

router = APIRouter()
TURN_CURVE_DIR = Path("logs") / "turn_curves"

# Global state to maintain active socket sessions
active_sessions = {}


def _lcp_words(a: list[str], b: list[str]) -> int:
    idx = 0
    upper = min(len(a), len(b))
    while idx < upper and a[idx].lower() == b[idx].lower():
        idx += 1
    return idx


def _tokenize_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", text or "")


def _build_word_timeline(transcript_events: list[dict]) -> list[dict]:
    """Build pseudo-word timing based on incremental Azure STT events."""
    words_timeline: list[dict] = []
    prev_words: list[str] = []

    for event in transcript_events:
        text = (event.get("text") or "").strip()
        if not text:
            continue

        current_words = _tokenize_words(text)
        if not current_words:
            continue

        lcp = _lcp_words(prev_words, current_words)
        if lcp == 0 and prev_words and current_words and current_words[0].lower() != prev_words[0].lower():
            # New hypothesis branch, do not force-match from old sequence.
            lcp = 0

        new_words = current_words[lcp:]
        t_rel = float(event.get("t_rel_sec", 0.0))

        for w in new_words:
            words_timeline.append(
                {
                    "t_rel_sec": t_rel,
                    "word": w,
                    "source_event": event.get("type", "PARTIAL"),
                }
            )

        prev_words = current_words
        if event.get("type") == "FINAL":
            prev_words = []

    return words_timeline


def _load_curve_payload(stream_sid: str) -> dict:
    file_path = TURN_CURVE_DIR / f"{stream_sid}.json"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"No curve log found for stream_sid={stream_sid}")
    return json.loads(file_path.read_text(encoding="utf-8"))


@router.get("/api/debug/turn-curves")
async def list_turn_curves():
    if not TURN_CURVE_DIR.exists():
        return JSONResponse({"sessions": []})

    sessions = []
    for p in sorted(TURN_CURVE_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            sessions.append(
                {
                    "stream_sid": payload.get("stream_sid", p.stem),
                    "tenant_id": payload.get("tenant_id"),
                    "session_start_epoch": payload.get("session_start_epoch"),
                    "session_end_epoch": payload.get("session_end_epoch"),
                    "curve_points": len(payload.get("curve", [])),
                    "transcript_events": len(payload.get("transcript_events", [])),
                    "file_name": p.name,
                }
            )
        except Exception:
            sessions.append(
                {
                    "stream_sid": p.stem,
                    "tenant_id": None,
                    "session_start_epoch": None,
                    "session_end_epoch": None,
                    "curve_points": 0,
                    "transcript_events": 0,
                    "file_name": p.name,
                    "parse_error": True,
                }
            )
    return JSONResponse({"sessions": sessions})


@router.get("/api/debug/turn-curves/{stream_sid}")
async def get_turn_curve(stream_sid: str):
    payload = _load_curve_payload(stream_sid)
    transcript_events = payload.get("transcript_events", [])
    payload["word_events"] = _build_word_timeline(transcript_events)
    return JSONResponse(payload)


@router.get("/debug/turn-taking", response_class=HTMLResponse)
async def turn_taking_debug_ui():
    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Turn Taking Debug</title>
  <style>
    :root {
      --bg: #f4f6f8;
      --card: #ffffff;
      --ink: #102028;
      --muted: #5a6a72;
      --line: #d6dde2;
      --accent: #0b7285;
      --accent2: #2b8a3e;
      --accent3: #d9480f;
    }
    body {
      margin: 0;
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      background: radial-gradient(circle at 20% 0%, #eaf2f5 0%, var(--bg) 55%);
      color: var(--ink);
    }
    .wrap {
      max-width: 1280px;
      margin: 0 auto;
      padding: 16px;
      display: grid;
      gap: 12px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 12px;
      box-shadow: 0 2px 14px rgba(0,0,0,0.04);
    }
    .row {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    select, button, input {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      background: #fff;
      color: var(--ink);
      font-size: 14px;
    }
    button { cursor: pointer; }
    .grid {
      display: grid;
      gap: 10px;
      grid-template-columns: 1fr;
    }
    .chart-box {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      background: #fff;
    }
    .chart-title {
      margin: 0 0 6px 0;
      font-size: 13px;
      color: var(--muted);
      letter-spacing: 0.2px;
    }
    canvas {
      width: 100%;
      height: 150px;
      display: block;
      background: linear-gradient(180deg, #fdfefe 0%, #f7fafb 100%);
      border-radius: 6px;
    }
    .meta {
      font-size: 12px;
      color: var(--muted);
    }
    pre {
      margin: 0;
      white-space: pre-wrap;
      font-size: 12px;
      color: #1f2e35;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card row">
      <strong>Turn Taking Debug Dashboard</strong>
      <select id="sessionSelect"></select>
      <button id="reloadBtn">Reload Sessions</button>
      <button id="openBtn">Load Session</button>
      <span id="meta" class="meta"></span>
    </div>

    <div class="card">
      <div class="grid" id="charts"></div>
    </div>

    <div class="card">
      <div class="chart-title">Transcript Events</div>
      <pre id="events"></pre>
    </div>
  </div>

  <script>
    const chartDefs = [
      { key: "turn_complete_score", label: "Turn Complete Score", color: "#0b7285", yMin: 0, yMax: 1 },
      { key: "s_score", label: "Silence Score", color: "#1971c2", yMin: 0, yMax: 1 },
      { key: "p_score", label: "Pitch Score", color: "#2f9e44", yMin: 0, yMax: 1 },
      { key: "e_score", label: "Energy Score", color: "#d9480f", yMin: 0, yMax: 1 },
      { key: "r_score", label: "Rate Slowdown Score", color: "#6741d9", yMin: 0, yMax: 1 },
      { key: "t_score", label: "Text Completion Score", color: "#c2255c", yMin: 0, yMax: 1 },
      { key: "es_score", label: "Energy Slope Score", color: "#c92a2a", yMin: 0, yMax: 1 },
      { key: "ps_score", label: "Pitch Slope Score", color: "#1864ab", yMin: 0, yMax: 1 },
      { key: "zcr_score", label: "ZCR Shift Score", color: "#5f3dc4", yMin: 0, yMax: 1 },
      { key: "tilt_score", label: "Spectral Tilt Score", color: "#087f5b", yMin: 0, yMax: 1 },
      { key: "silence_duration", label: "Silence Duration (sec)", color: "#495057", yMin: 0, yMax: null },
      { key: "energy_current", label: "Energy Current", color: "#e67700", yMin: 0, yMax: null },
      { key: "log_energy_current", label: "Log Energy Current", color: "#f08c00", yMin: null, yMax: null },
      { key: "pitch_current", label: "Pitch Current (Hz)", color: "#1c7ed6", yMin: 0, yMax: null },
      { key: "pitch_delta_hz", label: "Pitch Delta (Baseline - Current)", color: "#0c8599", yMin: null, yMax: null },
      { key: "energy_slope_300ms", label: "Energy Slope (300ms)", color: "#d9480f", yMin: null, yMax: null },
      { key: "pitch_slope_300ms", label: "Pitch Slope (300ms)", color: "#1d4ed8", yMin: null, yMax: null },
      { key: "zcr_current", label: "Zero Crossing Rate", color: "#7c3aed", yMin: 0, yMax: null },
      { key: "spectral_tilt_ratio", label: "Spectral Tilt Ratio", color: "#0f766e", yMin: 0, yMax: null },
      { key: "noise_floor_energy", label: "Noise Floor Energy", color: "#2b8a3e", yMin: 0, yMax: null },
      { key: "speech_ratio_current", label: "Speech Ratio (Energy/Noise)", color: "#5f3dc4", yMin: 0, yMax: null },
      { key: "voiced_ratio", label: "Voiced Ratio", color: "#2b8a3e", yMin: 0, yMax: 1 },
      { key: "transcript_rate_current", label: "Transcript Rate (chars/s)", color: "#e03131", yMin: 0, yMax: null },
      { key: "rate_score_raw", label: "Rate Score Raw", color: "#7048e8", yMin: 0, yMax: 1 },
      { key: "text_score_raw", label: "Text Score Raw", color: "#d6336c", yMin: 0, yMax: 1 },
      { key: "energy_slope_raw", label: "Energy Slope Raw", color: "#b02a37", yMin: 0, yMax: 1 },
      { key: "pitch_slope_raw", label: "Pitch Slope Raw", color: "#0b7285", yMin: 0, yMax: 1 },
      { key: "zcr_shift_raw", label: "ZCR Shift Raw", color: "#6741d9", yMin: 0, yMax: 1 },
      { key: "spectral_tilt_raw", label: "Spectral Tilt Raw", color: "#0c8599", yMin: 0, yMax: 1 }
    ];

    const sessionSelect = document.getElementById("sessionSelect");
    const reloadBtn = document.getElementById("reloadBtn");
    const openBtn = document.getElementById("openBtn");
    const chartsEl = document.getElementById("charts");
    const metaEl = document.getElementById("meta");
    const eventsEl = document.getElementById("events");

    function makeCanvasCard(label) {
      const box = document.createElement("div");
      box.className = "chart-box";
      const title = document.createElement("div");
      title.className = "chart-title";
      title.textContent = label;
      const canvas = document.createElement("canvas");
      canvas.width = 1180;
      canvas.height = 150;
      box.appendChild(title);
      box.appendChild(canvas);
      chartsEl.appendChild(box);
      return canvas;
    }

    function drawChart(canvas, points, def, maxT, wordEvents) {
      const ctx = canvas.getContext("2d");
      const w = canvas.width;
      const h = canvas.height;
      const padL = 44, padR = 10, padT = 18, padB = 24;
      const plotW = w - padL - padR;
      const plotH = h - padT - padB;

      ctx.clearRect(0, 0, w, h);
      ctx.strokeStyle = "#dce3e8";
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i++) {
        const y = padT + (plotH * i / 4);
        ctx.beginPath();
        ctx.moveTo(padL, y);
        ctx.lineTo(w - padR, y);
        ctx.stroke();
      }

      const vals = points.map(p => p[def.key]).filter(v => Number.isFinite(v));
      if (vals.length === 0) {
        ctx.fillStyle = "#7b8a92";
        ctx.fillText("no data", padL + 8, padT + 16);
        return;
      }

      let yMin = Number.isFinite(def.yMin) ? def.yMin : Math.min(...vals);
      let yMax = Number.isFinite(def.yMax) ? def.yMax : Math.max(...vals);
      if (yMax <= yMin) yMax = yMin + 1;

      const xOf = t => padL + (Math.max(0, t) / Math.max(1e-6, maxT)) * plotW;
      const yOf = v => padT + (1 - ((v - yMin) / (yMax - yMin))) * plotH;

      ctx.strokeStyle = def.color;
      ctx.lineWidth = 1.8;
      ctx.beginPath();
      let moved = false;
      for (const p of points) {
        const v = p[def.key];
        if (!Number.isFinite(v)) continue;
        const x = xOf(p.t_rel_sec || 0);
        const y = yOf(v);
        if (!moved) { ctx.moveTo(x, y); moved = true; }
        else { ctx.lineTo(x, y); }
      }
      ctx.stroke();

      // Second ticks on x-axis
      const tickCount = Math.max(4, Math.min(12, Math.floor(maxT)));
      ctx.strokeStyle = "#e6ecef";
      ctx.fillStyle = "#5e6d75";
      ctx.font = "10px Segoe UI";
      for (let i = 0; i <= tickCount; i++) {
        const sec = (i / tickCount) * maxT;
        const x = xOf(sec);
        ctx.beginPath();
        ctx.moveTo(x, padT);
        ctx.lineTo(x, h - padB);
        ctx.stroke();
        ctx.fillText(sec.toFixed(1) + "s", x - 12, h - 6);
      }

      // Word overlay on each graph (time-aligned)
      let lastLabelX = -9999;
      ctx.strokeStyle = "rgba(177, 151, 252, 0.35)";
      ctx.fillStyle = "#5f3dc4";
      ctx.font = "10px Segoe UI";
      for (const item of (wordEvents || [])) {
        const x = xOf(item.t_rel_sec || 0);
        ctx.beginPath();
        ctx.moveTo(x, padT);
        ctx.lineTo(x, h - padB);
        ctx.stroke();
        if (x - lastLabelX > 34) {
          ctx.fillText(item.word, x + 2, padT + 9);
          lastLabelX = x;
        }
      }

      ctx.fillStyle = "#51616a";
      ctx.font = "11px Segoe UI";
      ctx.fillText(yMax.toFixed(2), 4, padT + 4);
      ctx.fillText(yMin.toFixed(2), 4, h - padB + 2);
    }

    async function loadSessions() {
      const resp = await fetch("/api/debug/turn-curves");
      const data = await resp.json();
      sessionSelect.innerHTML = "";
      (data.sessions || []).forEach((s, i) => {
        const opt = document.createElement("option");
        opt.value = s.stream_sid;
        opt.textContent = `${s.stream_sid} | points=${s.curve_points} | events=${s.transcript_events}`;
        if (i === 0) opt.selected = true;
        sessionSelect.appendChild(opt);
      });
      if (!sessionSelect.value) {
        metaEl.textContent = "No sessions found in logs/turn_curves";
      }
    }

    async function openSession() {
      const sid = sessionSelect.value;
      if (!sid) return;
      const resp = await fetch(`/api/debug/turn-curves/${sid}`);
      if (!resp.ok) {
        metaEl.textContent = "Failed to load session.";
        return;
      }
      const data = await resp.json();
      const curve = data.curve || [];
      const events = data.transcript_events || [];
      const words = data.word_events || [];
      const maxT = Math.max(1, ...curve.map(c => c.t_rel_sec || 0), ...events.map(e => e.t_rel_sec || 0));

      chartsEl.innerHTML = "";
      chartDefs.forEach(def => {
        const cv = makeCanvasCard(def.label);
        drawChart(cv, curve, def, maxT, words);
      });
      eventsEl.textContent = JSON.stringify(events, null, 2);
      metaEl.textContent = `stream=${data.stream_sid} tenant=${data.tenant_id} points=${curve.length} words=${words.length}`;
    }

    reloadBtn.addEventListener("click", loadSessions);
    openBtn.addEventListener("click", openSession);

    (async () => {
      await loadSessions();
      await openSession();
      window.addEventListener("resize", () => openSession());
    })();
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html)

@router.post("/incoming-call")
async def handle_incoming_call(request: Request):
    """
    Twilio webhook for incoming calls. Returns TwiML.
    """
    host = request.headers.get("host", "localhost:8000")
    websocket_url = f"wss://{host}/media-stream"
    
    # Twilio sends data as an x-www-form-urlencoded payload
    form_data = await request.form()
    
    # Log the full form payload so you can inspect 'From', 'To', 'SipDomain', etc.
    logger.info(f"Incoming call webhook payload: {dict(form_data)}")
    
    # The identifier to look up in our fake DB. (E.g the number we called: form_data.get("To"))
    identifier = form_data.get("To", "default")
    
    # Look up this tenant in the dummy database
    tenant_config = get_tenant_config(identifier)
    logger.info(f"Resolved tenant config for '{identifier}': {tenant_config}")
    
    logger.info(f"Routing Twilio to Media Stream at: {websocket_url}")
    
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Connecting to your dynamic tenant service.</Say>
    <Connect>
        <Stream url="{websocket_url}">
            <Parameter name="tenant_id" value="{tenant_config['tenant_id']}" />
            <Parameter name="stt_engine" value="{tenant_config['stt_engine']}" />
            <Parameter name="languages" value="{tenant_config['languages']}" />
        </Stream>
    </Connect>
</Response>"""
    
    return Response(content=twiml_response, media_type="application/xml")


@router.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """
    Endpoint for receiving the Twilio media stream.
    """
    await websocket.accept()
    logger.info("Twilio WebSocket connected.")
    
    stream_sid = None
    
    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event_type = data.get("event")
            
            if event_type == "start":
                stream_sid = data.get("start", {}).get("streamSid")
                custom_parameters = data.get("start", {}).get("customParameters", {})
                
                logger.info(f"Twilio Event: Stream started ({stream_sid}). Parameters: {custom_parameters}")
                
                 # Retrieve the dynamically tracked tenant parameters out of the TwiML Stream mapping!
                tenant_id = custom_parameters.get("tenant_id", "default")
                stt_engine_str = custom_parameters.get("stt_engine", "azure")
                languages = custom_parameters.get("languages", "en-US,de-DE").split(",")
                
                # Setup loop bindings so Orchestrator can push commands down to this socket
                loop = asyncio.get_running_loop()
                async def send_twilio_command(payload: dict):
                    await websocket.send_json(payload)
                
                orchestrator = ConnectionOrchestrator(
                    stream_sid=stream_sid, 
                    tenant_id=tenant_id, 
                    stt_engine_str=stt_engine_str, 
                    languages=languages,
                    loop=loop,
                    send_command_cb=send_twilio_command
                )
                active_sessions[stream_sid] = orchestrator
                orchestrator.start()
                
            elif event_type == "media":
                if stream_sid and stream_sid in active_sessions:
                    payload = data.get("media", {}).get("payload", "")
                    active_sessions[stream_sid].process_media(payload)
                
            elif event_type == "stop":
                logger.info("Twilio Event: Stream stopped.")
                if stream_sid and stream_sid in active_sessions:
                    active_sessions[stream_sid].stop()
                    del active_sessions[stream_sid]
                break
                
    except WebSocketDisconnect:
        logger.info("Twilio WebSocket disconnected by client.")
        if stream_sid and stream_sid in active_sessions:
            active_sessions[stream_sid].stop()
            del active_sessions[stream_sid]
    except Exception as e:
        logger.error(f"Media stream error: {e}")
