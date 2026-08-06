# Source installation (Windows)

Setup.exe remains the recommended option for ordinary users. This workflow is for contributors and users who prefer Git-based updates.

## First installation

Install Python 3.11 and Git for Windows, open a terminal in a normal writable folder, then run:

```bat
git clone https://github.com/SeaBassBand/NovelAI-Artist-Ranker.git -b release
cd NovelAI-Artist-Ranker
Install.bat
```

`Install.bat` creates `.venv`, installs `requirements.lock.txt`, selects a data folder outside the repository, starts the local server, and opens the browser. Later, `Start.bat` reuses the environment and starts quickly.

## Updating

Close Artist Ranker and double-click:

```text
Update and Start.bat
```

The updater refuses to discard local modifications and uses `git pull --ff-only`. Source installations do not apply binary Update ZIPs over tracked files.

The signed Android APK is downloaded from the matching GitHub Release; it is not rebuilt during source installation.
