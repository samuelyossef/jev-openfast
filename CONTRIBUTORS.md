# Contributors

JEV OpenFast Browser exists thanks to the people and projects below.

## People and organizations

| Who | Contribution |
| --- | --- |
| [Browser Use](https://github.com/browser-use) | The original Jev Ultrafast project this one builds on: decision loop, DOM snapshot and first demos (MIT). |
| [Gregor Žunič](https://github.com/gregpr07) | Author of the original commits, kept in this repository's history. |
| [Samuel Yossef](https://github.com/samuelyossef) | Maintainer: chat, manual control, live preview, verification, history, documentation. |

## Projects this app depends on

| Project | What it provides | License |
| --- | --- | --- |
| [Browser Harness](https://github.com/browser-use/browser-harness) ([PyPI](https://pypi.org/project/browser-harness/)) | Chrome control through the DevTools Protocol: the daemon, the connection and the `cdp` helpers that every browser action goes through. Pinned in `pyproject.toml`. | MIT |
| [TypeSafe's Jev](https://openrouter.ai/typesafe/jev-1.13) ([docs](https://docs.typesafe.ai)) | The structured decision model (`typesafe/jev-1.13`) that picks each operation and target from typed questions. | service |
| [OpenRouter](https://openrouter.ai/) | Gateway to the models used for decisions, text and chat helpers. | service |
| [httpx](https://www.python-httpx.org/) | HTTP/2 client for model requests. | BSD-3-Clause |
| [React](https://react.dev/) and [Vite](https://vite.dev/) | The chat interface. | MIT |

## Want to be listed?

Contributions of any size count: code, tests, docs, bug reports and ideas. Read [CONTRIBUTING.md](CONTRIBUTING.md)
and open a pull request. The [contributors graph](https://github.com/samuelyossef/jev-openfast-browser/graphs/contributors)
on GitHub lists everyone who commits to the repository.
