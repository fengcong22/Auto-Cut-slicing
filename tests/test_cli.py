import json

from auto_cut_slicing.cli import main


def test_plan_cli_reports_real_compiled_outputs(tmp_path, capsys, monkeypatch):
    # The CLI orchestrates the same maintained compiler, with no upstream side effects.
    from auto_cut_slicing import planner

    job_path = tmp_path / "job.json"
    plan_path = tmp_path / "plan.json"
    plan = {
        "job_sha256": "fixture",
        "outputs": [{"id": "s1", "duration_us": 8000000, "segments": [{"id": "1"}, {"id": "2"}]}],
    }
    monkeypatch.setattr(planner, "compile_job", lambda path: plan)
    assert main(["plan", str(job_path), "--output", str(plan_path)]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["outputs"][0]["segment_count"] == 2
    assert json.loads(plan_path.read_text(encoding="utf-8")) == plan


def test_cli_emits_actionable_missing_media_error(tmp_path, capsys):
    job = tmp_path / "job.json"
    job.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "bad",
                "sources": [{"id": "s", "path": "missing.mp4"}],
                "outputs": [
                    {"id": "o", "clips": [{"id": "c", "source": "s", "start": 0, "end": 1}]}
                ],
            }
        ),
        encoding="utf-8",
    )
    assert main(["plan", str(job), "--output", str(tmp_path / "plan.json")]) == 1
    response = json.loads(capsys.readouterr().err)
    assert response["ok"] is False
    assert not (tmp_path / "plan.json").exists()


def test_plan_cannot_overwrite_source_or_input(tmp_path, capsys):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original source bytes")
    assert main(["plan", "unused-job.json", "--output", str(source)]) == 1
    assert source.read_bytes() == b"original source bytes"
    assert "overwrite" in capsys.readouterr().err


def test_receipt_cannot_replace_draft_content(tmp_path, capsys, monkeypatch):
    from auto_cut_slicing import planner

    monkeypatch.setattr(
        planner,
        "compile_job",
        lambda path: {"job_sha256": "fixture", "outputs": [{"id": "s1", "name": "Draft"}]},
    )
    assert (
        main(
            [
                "draft",
                "unused-job.json",
                "--drafts-root",
                str(tmp_path),
                "--receipt",
                str(tmp_path / "Draft" / "draft_content.json"),
            ]
        )
        == 1
    )
    assert "outside" in capsys.readouterr().err
    assert not (tmp_path / "Draft").exists()


def test_failed_batch_receipt_preserves_partial_success_without_claiming_complete(
    tmp_path, capsys, monkeypatch
):
    from auto_cut_slicing import drafts, planner, upstream
    from auto_cut_slicing.errors import SlicingError

    monkeypatch.setattr(
        planner,
        "compile_job",
        lambda path: {
            "job_sha256": "fixture",
            "outputs": [{"id": "one", "name": "One"}, {"id": "two", "name": "Two"}],
        },
    )
    monkeypatch.setattr(upstream, "load_config", lambda path: {})

    def writer(plan, output_id, root, *, config):
        if output_id == "two":
            raise SlicingError("attributable second draft failure")
        return {"output_id": output_id}

    monkeypatch.setattr(drafts, "write_draft", writer)
    receipt_path = tmp_path / "receipt.json"
    assert (
        main(
            [
                "draft",
                "unused-job.json",
                "--drafts-root",
                str(tmp_path / "drafts"),
                "--receipt",
                str(receipt_path),
            ]
        )
        == 1
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["ok"] is False and receipt["status"] == "failed"
    assert receipt["drafts"] == [{"output_id": "one"}]
