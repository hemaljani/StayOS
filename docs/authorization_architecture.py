"""Render StayOS authorization using official AWS icons and Graphviz.

Run `python3 docs/authorization_architecture.py`. The official icon ZIP is read
in memory; only the diagram's durable icon assets, PNG, and standalone SVG are
written. This avoids dependency installation and local temporary files.
"""

import base64
import io
import subprocess
import urllib.request
import zipfile
from pathlib import Path

DOCS = Path(__file__).resolve().parent
ICON_PACKAGE = (
    "https://d1.awsstatic.com/onedam/marketing-channels/website/public/shared/"
    "architecture-icon-release/"
    "Icon-package_07312026.5846e92413caa21490223536cc97f1269e44fa92.zip"
)
ICONS = {
    "cognito": "Arch_Amazon-Cognito_64.png",
    "gateway": "Arch_Amazon-API-Gateway_64.png",
    "lambda": "Arch_AWS-Lambda_64.png",
    "secret": "Arch_AWS-Secrets-Manager_64.png",
    "cdn": "Arch_Amazon-CloudFront_64.png",
    "s3": "Arch_Amazon-Simple-Storage-Service_64.png",
    "agents": "Arch_Amazon-Bedrock-AgentCore_64.png",
}


def main():
    assets = DOCS / "authorization-icons"
    assets.mkdir(exist_ok=True)
    if not all((assets / name).exists() for name in ICONS.values()):
        package = urllib.request.urlopen(ICON_PACKAGE, timeout=30).read()
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            for filename in ICONS.values():
                member = next(
                    name for name in archive.namelist()
                    if not name.startswith("__MACOSX/") and name.endswith("/" + filename)
                )
                (assets / filename).write_bytes(archive.read(member))
    labels = {
        "cognito": "Amazon Cognito\\nExisting pool + client",
        "gateway": "API Gateway\\nVerified ID token / TLS 1.2",
        "lambda": "Existing API Lambda\\nProperty authorization + signer",
        "secret": "Secrets Manager\\nNew private signing-key secret",
        "cdn": "Audio CloudFront\\nNew public key + key group",
        "s3": "Existing private S3\\nAudio brief objects",
        "agents": "Existing AgentCore runtimes\\nChat + voice / subject binding",
    }
    nodes = "\n".join(
        f'{key} [label="{labels[key]}", image="{assets / filename}", '
        'imagepos="tc", labelloc="b", width="2.6", height="1.35"];'
        for key, filename in ICONS.items()
    )
    graph = (
        'digraph StayOS { graph [rankdir=LR, bgcolor="white", pad="0.3", '
        'nodesep="0.45", ranksep="0.7", fontname="Arial", '
        'label="StayOS content and identity authorization", '
        'labelloc="t", fontsize="20"]; '
        'node [shape=none, fontname="Arial", fontsize="12"]; '
        'edge [fontname="Arial", fontsize="10", color="#526477"]; '
        + nodes
        + '''
        browser [label="Signed-in browser", shape=box, color="#526477"];
        browser -> cognito [label="Existing sign-in"];
        browser -> gateway [label="ID token"];
        gateway -> lambda [label="Verified claims"];
        secret -> lambda [label="CloudFormation resolves key", style=dashed];
        lambda -> browser [label="Signed object URL / 900 seconds", constraint=false];
        browser -> cdn [label="Signed audio URL"];
        cdn -> s3 [label="Origin Access Control"];
        browser -> agents [label="OAuth access token"];
        cognito -> agents [label="Fixed issuer / client + JWKS", style=dashed];
        }
        '''
    )
    stem = DOCS / "authorization-architecture"
    for fmt in ("png", "svg"):
        data = subprocess.run(
            ["dot", f"-T{fmt}"], input=graph.encode(), capture_output=True, check=True,
        ).stdout
        if fmt == "svg":
            # Embed icons so the final SVG can be opened or shared on its own.
            text = data.decode()
            for filename in ICONS.values():
                path = assets / filename
                text = text.replace(
                    str(path),
                    "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode(),
                )
            data = text.encode()
        stem.with_suffix("." + fmt).write_bytes(data)
    print("Rendered durable authorization diagram and official icon assets.")


if __name__ == "__main__":
    main()
