#!/usr/bin/env python3
"""Render every PlantUML source in report/uml/ to a PNG in report/Figures/.

    ./robofetch_venv/bin/python tools/report/make_uml.py

The .puml files are the editable originals. Change one, re-run this, and the
figure in the report updates - the LaTeX only ever does \\includegraphics.

You can also edit them without this script at all: paste a .puml file into
https://www.plantuml.com/plantuml and download the PNG, or install the
PlantUML extension for VS Code and preview them inline.
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPORT = HERE.parent.parent / "report"
UML_DIR = REPORT / "uml"
FIG_DIR = REPORT / "Figures"
JAR = HERE / "plantuml.jar"


def main():
    if not JAR.exists():
        print(f"plantuml.jar not found at {JAR}")
        print("Download it from https://plantuml.com/download and put it there,")
        print("or render the .puml files online at https://www.plantuml.com/plantuml")
        return 1

    FIG_DIR.mkdir(parents=True, exist_ok=True)
    sources = sorted(UML_DIR.glob("*.puml"))
    if not sources:
        print(f"no .puml files in {UML_DIR}")
        return 1

    print(f"rendering {len(sources)} diagrams -> {FIG_DIR}")
    result = subprocess.run(
        ["java", "-jar", str(JAR), "-tpng", "-o", str(FIG_DIR), str(UML_DIR)],
        capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        return result.returncode

    failed = []
    for source in sources:
        png = FIG_DIR / f"{source.stem}.png"
        if png.exists():
            print(f"  ok    {png.name:34} {png.stat().st_size / 1024:6.1f} kB")
        else:
            failed.append(source.name)
            print(f"  FAIL  {source.name}")

    if failed:
        print(f"\n{len(failed)} diagram(s) did not render: {', '.join(failed)}")
        return 1
    print(f"\nall {len(sources)} diagrams rendered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
