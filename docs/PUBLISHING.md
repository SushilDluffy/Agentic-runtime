# Publishing Agentic Runtime to PyPI

The initial publication target is `agentic-runtime==0.1.1`, marked Alpha/experimental. An installed package uses `import agentic_runtime`. The repository's examples are run from a checkout. Publication does not change the single-host scope or establish production reliability.

## One-time owner setup

1. Sign in to [PyPI](https://pypi.org/account/login/), verify your email and enable two-factor authentication if not already configured.
2. Open [account Publishing](https://pypi.org/manage/account/publishing/) and add a **pending publisher** for a new project, selecting GitHub Actions:

   | Field | Exact value |
   | --- | --- |
   | PyPI project name | `agentic-runtime` |
   | Owner | `SushilDluffy` |
   | Repository | `Agentic-runtime` |
   | Workflow filename | `publish.yml` |
   | Environment name | `pypi` |

3. In [GitHub repository environments](https://github.com/SushilDluffy/Agentic-runtime/settings/environments), create an environment named `pypi` if it does not exist. You can restrict deployment branches to `main`; the workflow also refuses publication from other branches.

No PyPI API token or GitHub secret is needed. The PyPI project is created under the account that registers the pending publisher when the first upload succeeds. A pending publisher does not reserve the name. The public JSON endpoint returned 404 on November 18, 2026; name availability is ultimately determined by PyPI at publication. If PyPI rejects the name as unavailable or reserved, stop and choose a distinct name before changing metadata and publisher configuration. Do not overwrite an unrelated project.

If this project already exists under your account, configure its ordinary Trusted Publisher from the project's Publishing settings using the same GitHub fields instead.

## Publish

1. Open [Publish to PyPI](https://github.com/SushilDluffy/Agentic-runtime/actions/workflows/publish.yml).
2. Select **Run workflow**, branch **main**, and enable **Publish the validated package to production PyPI**.
3. The workflow runs lint, formatting, tests and the scripted comparison on Python 3.11 and 3.12. After both pass, it builds the wheel and source archive and checks their metadata/README with `twine check --strict`.
4. A separate job downloads those exact artifacts and publishes using GitHub OIDC and the `pypi` environment. Only this job receives `id-token: write`. If the environment has required reviewers, its configured reviewers must approve the deployment.

The checkbox defaults to false so a manual validation-only run makes no upload. A change to the publishing workflow on `main` automatically validates/builds it, but does not publish. A failing validation/build prevents the publishing job from running. Only artifacts from the current run are uploaded, and concurrent publication runs are serialized.

After success, verify the version and repository links at [PyPI](https://pypi.org/project/agentic-runtime/), then install in a fresh environment:

```bash
python -m pip install 'agentic-runtime==0.1.1'
python -c 'import agentic_runtime; print(agentic_runtime.__version__)'
```

Optional extras include `anthropic`, `openai`, `web`, and `github`. Do not announce the package as published until the upload and fresh installation have succeeded. Future releases require a new version in `pyproject.toml` and `src/agentic_runtime/__init__.py`, refreshed `uv.lock`, and passing checks; PyPI does not allow reusing an uploaded distribution filename.

## Official references

- [Create a project through a pending Trusted Publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
- [Use Trusted Publishing from GitHub Actions](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
- [PyPI account requirements](https://docs.pypi.org/your-account/managing-your-account/)
