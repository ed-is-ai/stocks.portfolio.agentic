## ADDED Requirements

### Requirement: Pipeline stages compose through a statically-typed contract

The pipeline SHALL be assembled from `Step` adapters via a `Pipeline` builder
whose `.then()` advances the output type while pinning the input type, so that a
mis-ordered stage is a type error at the wiring site rather than a runtime
failure.

#### Scenario: Correct wiring type-checks

- **WHEN** Steps are chained `start(ScanStep).then(AnalyseStep).then(AlertStep)`
  where each Step's input type equals the previous Step's output type
- **THEN** `pyrefly check` reports no error
- **AND** `Pipeline.run(payload)` returns the final Step's output type

#### Scenario: Mis-wiring is a type error

- **WHEN** a Step is appended whose input type does not match the current
  pipeline output type
- **THEN** `pyrefly check` reports a type error at the `.then()` call

### Requirement: Agents stay pure and ignorant of the pipeline

Pipeline-stage agents SHALL expose `run(payload) -> result` and SHALL NOT import
the pipeline, the Step adapters, or each other. The typed contract SHALL live in
the Step adapters under `app/workflows/`.

#### Scenario: Agent has no pipeline knowledge

- **WHEN** a pipeline-stage agent module is inspected
- **THEN** it does not import `app.workflows`
- **AND** it does not import another agent package

#### Scenario: Step adapter owns the boundary type

- **WHEN** a Step adapter runs
- **THEN** it declares the typed input/output and delegates to the agent's
  `run`

### Requirement: Validation is static-only

The pipeline SHALL rely on static type checking for the stage contract and SHALL
NOT re-validate stage payloads with Pydantic at each boundary at runtime.

#### Scenario: No runtime re-validation between stages

- **WHEN** the pipeline passes one stage's output to the next
- **THEN** it does so without re-parsing or re-validating the payload model

### Requirement: Linear pipeline preserves per-stage trace and replaces AgentApp

The `Pipeline` SHALL be linear and SHALL replace `ms_agent_framework.AgentApp`.
It SHALL provide a traced run exposing each stage's name and output so the Excel
export can consume intermediates, and SHALL record per-stage timing.

#### Scenario: Traced run exposes intermediates

- **WHEN** `Pipeline.run_traced(payload)` completes
- **THEN** it returns the final output and an ordered list of
  `(stage_name, stage_output)` for every stage

#### Scenario: AgentApp is removed

- **WHEN** the change is complete
- **THEN** `ms_agent_framework.AgentApp` is no longer used
- **AND** pipeline wiring lives in `app/workflows/momentum.py`, not inline in
  `orchestrator.py`
