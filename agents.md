# Agents

## Changelog Generator

Responsible for generating and maintaining `CHANGELOG.md` after every task.

### Trigger

Run after every completed task — code changes, refactors, config updates, dependency changes, bug fixes, or any staged git diff.

### Instructions

1. Read the current git staged diff (`git diff --cached`) or the last commit diff (`git diff HEAD~1`) to understand what changed.
2. Read the existing `CHANGELOG.md` to understand the current format and latest version.
3. Generate a new changelog entry following [Keep a Changelog](https://keepachangelog.com/) format with these sections as applicable:
   - **Added** — new files, features, configs, dependencies
   - **Changed** — modifications to existing files, refactors, config updates
   - **Removed** — deleted files, removed dependencies, deprecated features
   - **Fixed** — bug fixes
   - **Security** — vulnerability patches
4. Use semantic versioning (`MAJOR.MINOR.PATCH`):
   - **MAJOR** — breaking changes (e.g., restructured Docker setup, renamed services)
   - **MINOR** — new features or services added
   - **PATCH** — bug fixes, small config tweaks, dependency bumps
5. Prepend the new entry to `CHANGELOG.md` under a new version header with today's date.
6. Include an **Estimated Impact** table when the change affects build size, performance, or operational complexity.
7. Keep descriptions concise but specific — reference actual file paths and concrete values (image names, sizes, counts).

### Output

Updated `CHANGELOG.md` at the project root.

---

## Docker Engineer

Responsible for maintaining the unified Docker setup across all 3 services.

### Scope

- Root `Dockerfile` (multi-stage build)
- Root `docker-compose.yml` (all services)
- Root `requirements.txt` (merged dependencies)

### Instructions

1. When adding or modifying a service, update the root `docker-compose.yml` — do **not** create per-service compose files.
2. When adding Python dependencies, add them to the root `requirements.txt` under the correct app comment section.
3. Maintain the multi-stage build pattern: compile C extensions in the builder stage, keep the final image slim.
4. All services must use the `tg_net` external network.
5. Set `PYTHONUNBUFFERED=1` for real-time log output.
6. Use `working_dir` and `command` overrides in compose — the Dockerfile stays generic.
7. After any Docker changes, trigger the **Changelog Generator** agent.

### Services

| Service | Container | Entry Point | Restart Policy |
|---------|-----------|-------------|----------------|
| `tg-comment` | `tg-comment` | `python anidl.py` | `on-failure` |
| `anisearch` | `tgsearch` | `python anidlsearch.py` | `on-failure` |
| `mongosync` | `python-mongo-sync` | `python app.py` | `always` |

---

## Code Reviewer

Reviews code changes before commit for correctness, security, and consistency.

### Instructions

1. Check that no secrets, API keys, or session files are staged.
2. Verify `.gitignore` covers generated files (`*.session`, `*.pyc`, `__pycache__/`, `venv/`, `logs/`, `*.db`).
3. Ensure `requirements.txt` has no duplicate or unnecessary dependencies (e.g., `dataclasses` on Python 3.7+).
4. Validate `docker-compose.yml` syntax and that all volume mounts point to correct paths.
5. Confirm Dockerfile best practices: minimal layers, no cache left in final image, slim base.
6. After review, trigger the **Changelog Generator** agent to document the changes.
