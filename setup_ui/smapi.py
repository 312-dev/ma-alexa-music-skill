"""Everything that shells out to the ASK CLI, behind one function.

`ask` is the only route to SMAPI here. Its credentials come from a browser
OAuth round trip and land in ~/.ask/cli_config, and there is no documented way
to mint them headlessly, which is the whole reason this wizard is a web page
rather than a script.

Every invocation goes through run(), for two reasons: tests replace exactly one
thing, and no code path can accidentally build a shell string out of a value
the user typed. run() takes an argv list and nothing else.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import time
from dataclasses import dataclass

CLI_CONFIG = pathlib.Path(
    os.environ.get("ASK_CONFIG", str(pathlib.Path.home() / ".ask" / "cli_config"))
)


@dataclass
class Result:
    argv: list[str]
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def message(self) -> str:
        return (self.stderr.strip() or self.stdout.strip())[:500]

    def data(self):
        """Parsed stdout, or None. Most smapi subcommands answer with JSON."""
        text = self.stdout.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            # Some subcommands print a human line before the JSON body.
            start = min(
                (i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1
            )
            if start < 0:
                return None
            try:
                return json.loads(text[start:])
            except ValueError:
                return None


def run(argv: list[str], timeout: int = 120) -> Result:
    """Run one CLI command. The single seam every test monkeypatches."""
    if isinstance(argv, str):
        raise TypeError("argv must be a list, never a shell string")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return Result(argv, 127, "", f"{argv[0]} not found on PATH")
    except subprocess.TimeoutExpired:
        return Result(argv, 124, "", f"{argv[0]} timed out after {timeout}s")
    return Result(argv, proc.returncode, proc.stdout or "", proc.stderr or "")


# --- environment ------------------------------------------------------------


def ask_on_path() -> bool:
    return shutil.which("ask") is not None


def ask_configured() -> bool:
    try:
        return bool(json.loads(CLI_CONFIG.read_text()).get("profiles"))
    except (OSError, ValueError, AttributeError):
        return False


# --- thin wrappers ----------------------------------------------------------


def list_vendors() -> Result:
    return run(["ask", "smapi", "get-vendor-list"])


def create_skill(manifest_path: str, vendor_id: str) -> Result:
    return run([
        "ask", "smapi", "create-skill-for-vendor",
        "--vendor-id", vendor_id,
        "--manifest", f"file:{manifest_path}",
    ], timeout=300)


def update_manifest(skill_id: str, manifest_path: str, stage: str = "development") -> Result:
    return run([
        "ask", "smapi", "update-skill-manifest",
        "-s", skill_id, "-g", stage,
        "--manifest", f"file:{manifest_path}",
    ], timeout=300)


def skill_status(skill_id: str) -> Result:
    return run(["ask", "smapi", "get-skill-status", "-s", skill_id])


def create_catalog(title: str, catalog_type: str, vendor_id: str) -> Result:
    return run([
        "ask", "smapi", "create-catalog",
        "--title", title,
        "--type", catalog_type,
        "--usage", "AlexaMusic.Catalog.Reference",
        "--vendor-id", vendor_id,
    ])


def associate_catalog(catalog_id: str, skill_id: str) -> Result:
    return run([
        "ask", "smapi", "associate-catalog-with-skill",
        "-c", catalog_id, "-s", skill_id,
    ])


def upload_catalog(catalog_id: str, path: str) -> Result:
    return run(
        ["ask", "smapi", "upload-catalog", "-c", catalog_id, "-f", path],
        timeout=900,
    )


def list_uploads(catalog_id: str) -> Result:
    return run(["ask", "smapi", "list-uploads-for-catalog", "-c", catalog_id])


def upload_detail(catalog_id: str, upload_id: str) -> Result:
    return run([
        "ask", "smapi", "get-content-upload-by-id",
        "-c", catalog_id, "--upload-id", upload_id,
    ])


def enablement_status(skill_id: str, stage: str = "development") -> Result:
    return run(["ask", "smapi", "get-skill-enablement-status", "-s", skill_id, "-g", stage])


def set_enablement(skill_id: str, stage: str = "development") -> Result:
    return run(["ask", "smapi", "set-skill-enablement", "-s", skill_id, "-g", stage])


def delete_enablement(skill_id: str, stage: str = "development") -> Result:
    return run(["ask", "smapi", "delete-skill-enablement", "-s", skill_id, "-g", stage])


def cycle_enablement(skill_id: str, stage: str = "development") -> list[Result]:
    """Delete then set. A catalog upload silently unbinds the skill.

    Symptom: every diagnostic reports healthy, ER_INGESTION says SUCCEEDED, the
    skill answers every request correctly, and Alexa announces "Here's ... from
    Spotify" because playback fell back to the default provider. This is the
    only fix, and nothing else in the pipeline hints that it is needed.
    """
    return [delete_enablement(skill_id, stage), set_enablement(skill_id, stage)]


# --- classification ---------------------------------------------------------


def _steps(upload: dict) -> dict[str, str]:
    raw = upload.get("ingestionSteps") or upload.get("steps") or []
    out = {}
    for step in raw:
        if not isinstance(step, dict):
            continue
        name = step.get("name") or step.get("stepName") or ""
        out[str(name).upper()] = str(step.get("status") or "").upper()
    return out


def classify_ingestion(upload: dict | None) -> dict:
    """Turn an upload record into the one thing the operator actually needs.

    Four situations look identical from outside the console, and only one of
    them means voice works:

    ER_INGESTION is the gate. Once it says SUCCEEDED, Alexa can resolve speech
    against the catalog and playback works. Everything else in the record is
    noise, and reading it as though it mattered is what makes people wait days
    for a skill that has been working the whole time.

    SLU_MODELING sits at PENDING for weeks. It never blocks playback. Because
    the top-level status is the minimum across all steps, it also pins that
    status at IN_PROGRESS for exactly as long, which is why the top-level
    status carries no information at all.
    """
    if not upload:
        return {
            "state": "none",
            "voice_ready": False,
            "er": "", "slu": "", "top": "",
            "headline": "No catalog uploaded yet",
            "detail": "Alexa has nothing to resolve names against, so it will "
                      "hand every request to the default provider.",
        }

    steps = _steps(upload)
    er = steps.get("ER_INGESTION", "")
    slu = steps.get("SLU_MODELING", "")
    top = str(upload.get("status") or "").upper()

    if er == "SUCCEEDED":
        detail = "Voice resolution is live."
        if slu and slu != "SUCCEEDED":
            detail += (
                f" SLU_MODELING is {slu}, which is normal and takes weeks. It never"
                " blocks playback."
            )
        if top and top != "SUCCEEDED":
            detail += (
                f" The top-level status reads {top} only because it is pinned by"
                " SLU_MODELING. Ignore it."
            )
        return {"state": "ok", "voice_ready": True, "er": er, "slu": slu, "top": top,
                "headline": "ER_INGESTION SUCCEEDED", "detail": detail}

    if er in ("FAILED", "ERROR"):
        return {"state": "failed", "voice_ready": False, "er": er, "slu": slu, "top": top,
                "headline": "ER_INGESTION FAILED",
                "detail": "The catalog was rejected. Alexa is resolving against "
                          "whatever it last accepted, which may be nothing."}

    return {"state": "waiting", "voice_ready": False, "er": er or "UNKNOWN",
            "slu": slu, "top": top,
            "headline": f"ER_INGESTION {er or 'UNKNOWN'}",
            "detail": "Still ingesting. Voice will not resolve library names "
                      "until this reaches SUCCEEDED. Usually minutes, not days."}


def latest_upload(catalog_id: str) -> dict | None:
    listing = list_uploads(catalog_id).data() or {}
    uploads = listing.get("uploads") or listing.get("contentUploads") or []
    if not uploads:
        return None
    newest = max(
        uploads,
        key=lambda u: str(u.get("createdDate") or u.get("lastUpdatedDate") or ""),
    )
    upload_id = newest.get("id") or newest.get("uploadId")
    if not upload_id:
        return newest
    return upload_detail(catalog_id, upload_id).data() or newest


def classify_enablement(result: Result) -> dict:
    """Enablement is the failure mode with no symptom anywhere else.

    Without it the skill is never routed to. Alexa answers "Here's ... from
    Spotify" while the endpoint, the catalog and the manifest all report
    healthy, so nothing points at the cause.
    """
    if result.ok:
        return {"enabled": True, "headline": "Enabled for development",
                "detail": "Alexa will route to this skill."}
    body = (result.stdout + result.stderr).lower()
    if "not enabled" in body or "404" in body or "notfound" in body:
        detail = ("Alexa will silently use the default provider and announce it "
                  "out loud, while every other check here still reads healthy. "
                  "Run set-skill-enablement, stage development.")
    else:
        detail = result.message or "Could not read enablement status."
    return {"enabled": False, "headline": "Not enabled", "detail": detail}


# --- manifest ---------------------------------------------------------------


def manifest(*, name: str, public_base: str, cert_type: str,
             summary: str = "", vendor_email: str = "") -> dict:
    """A music-skill manifest.

    Two things here are not in Amazon's music-skill documentation and both were
    found the hard way. The endpoint may be an HTTPS URI rather than a Lambda
    ARN, and when it is, sslCertificateType is required: SMAPI rejects the
    manifest with MISSING_REQUIRED_PROPERTY otherwise. Getting the certificate
    type wrong is worse than omitting it, because the manifest is accepted and
    Amazon simply never calls the endpoint.
    """
    base = public_base.rstrip("/")
    return {
        "manifest": {
            "manifestVersion": "1.0",
            "publishingInformation": {
                "locales": {
                    "en-US": {
                        "name": name,
                        "summary": summary or "Play your own music library on Alexa.",
                        "description": (
                            f"{name} plays a self-hosted Subsonic-compatible music "
                            "library on Echo devices, with Alexa's native player."
                        ),
                        "examplePhrases": [
                            f"Alexa, play Radiohead on {name.lower()}",
                            f"Alexa, ask {name.lower()} to play my bedtime playlist",
                        ],
                        "keywords": ["music", "subsonic", "self-hosted"],
                        "smallIconUri": f"{base}/icons/ampere-108.png",
                        "largeIconUri": f"{base}/icons/ampere-512.png",
                    }
                },
                "isAvailableWorldwide": False,
                "distributionCountries": ["US"],
                "distributionMode": "PRIVATE",
                "category": "MUSIC_AND_AUDIO",
                "testingInstructions": (
                    "Private development-stage music skill. Requires account "
                    "linking against the operator's own bridge."
                ),
            },
            "apis": {
                "music": {
                    "endpoint": {
                        "uri": f"{base}/music",
                        "sslCertificateType": cert_type,
                    },
                    "interfaces": [
                        {"namespace": "Alexa.Media.Search", "version": "1.0"},
                        {"namespace": "Alexa.Media.Playback", "version": "1.0"},
                        {"namespace": "Alexa.Media.PlayQueue", "version": "1.0"},
                        {"namespace": "Alexa.Audio.PlayQueue", "version": "1.0"},
                    ],
                    "locales": {"en-US": {}},
                    "regions": {"NA": {"endpoint": {
                        "uri": f"{base}/music",
                        "sslCertificateType": cert_type,
                    }}},
                }
            },
            "privacyAndCompliance": {
                "allowsPurchases": False,
                "usesPersonalInfo": False,
                "isChildDirected": False,
                "isExportCompliant": True,
                "containsAds": False,
                "locales": {"en-US": {
                    "privacyPolicyUrl": f"{base}/privacy",
                    "termsOfUseUrl": f"{base}/terms",
                }},
            },
        }
    }


def write_manifest(body: dict, directory: str) -> str:
    path = pathlib.Path(directory) / f"manifest-{int(time.time())}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2))
    return str(path)
