"""Contract checks for both self-contained Tauri chat overlays."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OVERLAYS = (
    ROOT / "frontend" / "src-tauri" / "src" / "overlay.html",
    ROOT / "desktop" / "src-tauri" / "src" / "overlay.html",
)


def test_overlays_require_provider_stop_and_done_before_complete() -> None:
    sources = [path.read_text(encoding="utf-8") for path in OVERLAYS]

    assert sources[0] == sources[1]
    for source in sources:
        assert "if(d==='[DONE]'){sawDone=true;break streamLoop}" in source
        assert "if(choice?.finish_reason!=null)terminal=choice.finish_reason" in source
        assert "if(!sawDone||terminal!=='stop'||terminalError)" in source
        assert "incomplete:!complete" in source
        assert "if(m.incomplete)o.incomplete=true" in source


def test_overlays_do_not_reintroduce_hidden_generation_limits() -> None:
    for path in OVERLAYS:
        source = path.read_text(encoding="utf-8")
        request_line = "body:JSON.stringify({model,messages,stream:true})"
        assert request_line in source
        request_window = source[
            source.index(request_line) : source.index(request_line) + 100
        ]
        assert "max_tokens" not in request_window
