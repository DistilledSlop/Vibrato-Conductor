# Vibrato Conductor

Vibrato Conductor will run and monitor a fleet of Minecraft 1.21.1 clients using the Vibrato mod to collect training data through the Protocol Transformer recording proxy.

Each worker container must include a Minecraft client running Vibrato, a recording proxy, and a Fabric server with an isolated world. The conductor will maintain the requested number of workers, distribute tasks across them, replace unhealthy workers, and collect packet captures in central storage. Docker will provide the worker runtime and Kubernetes will provide fleet scheduling and resource management.

The system also needs a diagnostic testing mode to identify problems such as stuck bots, repeated task loops, failed nerd poles, and failures during long tasks such as `@nether` and `@gamer`, with enough evidence to fix those problems before large training runs.

See [the system requirements](docs/requirements.md) for the requested behavior, related projects, and implementation starting points.

Current status: requirements and initial project inspection only. The conductor has not been implemented or validated in Minecraft yet.
