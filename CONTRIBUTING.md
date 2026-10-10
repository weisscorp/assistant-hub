# Contributing

Keep Hub coordination and Android device execution in their separate repositories.
Use neutral synthetic people, locations and facts in examples. Run the backend
unit/HTTP suites; they use local temporary state and do not need cloud credentials.
Do not include personal history, screenshots, generated runtime config, models
or tokens. For new providers, add meaningful fixtures covering tool results,
errors, cancellation and terminal state. Preserve original dependency credits.

Changes to model prompts are not a substitute for capability checks or executors.
Document actual tested behavior and gaps. Do not automatically install or start
applications on physical devices as part of development or CI.
