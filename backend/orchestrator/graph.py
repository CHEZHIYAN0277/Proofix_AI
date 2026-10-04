from langgraph.graph import END, StateGraph

from backend.config import Settings
from backend.orchestrator.edges import (
    after_environment,
    after_mutation,
    after_security,
    should_reinvestigate,
)
from backend.orchestrator.nodes import GraphNodes
from backend.state.redis_store import RedisStore
from backend.state.schema import RunState


def build_graph(store: RedisStore, settings: Settings) -> StateGraph:
    nodes = GraphNodes(store, settings)

    graph = StateGraph(RunState)

    async def prepare_repo(state: RunState) -> RunState:
        from backend.state.schema import RunStateModel
        model = RunStateModel(**{k: v for k, v in state.items() if k in RunStateModel.model_fields})
        result = await nodes.prepare_repo(model)
        return result.model_dump(exclude_none=False)

    async def environment_precheck(state: RunState) -> RunState:
        model = await _load_model(store, state)
        result = await nodes.environment_precheck(model)
        return result.model_dump(exclude_none=False)

    async def halt_environment(state: RunState) -> RunState:
        model = await _load_model(store, state)
        result = await nodes.halt_environment(model)
        return result.model_dump(exclude_none=False)

    async def index_repository(state: RunState) -> RunState:
        model = await _load_model(store, state)
        result = await nodes.index_repository(model)
        return result.model_dump(exclude_none=False)

    async def parallel_intel(state: RunState) -> RunState:
        model = await _load_model(store, state)
        result = await nodes.parallel_intel(model)
        return result.model_dump(exclude_none=False)

    async def layer1_fan_in(state: RunState) -> RunState:
        model = await _load_model(store, state)
        result = await nodes.layer1_fan_in(model)
        return result.model_dump(exclude_none=False)

    async def reproduction_gate(state: RunState) -> RunState:
        model = await _load_model(store, state)
        result = await nodes.reproduction_gate(model)
        return result.model_dump(exclude_none=False)

    async def investigate(state: RunState) -> RunState:
        model = await _load_model(store, state)
        result = await nodes.investigate(model)
        return result.model_dump(exclude_none=False)

    async def blast_scope(state: RunState) -> RunState:
        model = await _load_model(store, state)
        result = await nodes.blast_scope(model)
        return result.model_dump(exclude_none=False)

    async def engineer_context(state: RunState) -> RunState:
        model = await _load_model(store, state)
        result = await nodes.engineer_context(model)
        return result.model_dump(exclude_none=False)

    async def plan_fixes(state: RunState) -> RunState:
        model = await _load_model(store, state)
        result = await nodes.plan_fixes(model)
        return result.model_dump(exclude_none=False)

    async def generate_code(state: RunState) -> RunState:
        model = await _load_model(store, state)
        model.retry_count = state.get("retry_count", model.retry_count)
        model.retry_brief = state.get("retry_brief")
        model.validation_failure = state.get("validation_failure")
        result = await nodes.generate_code(model)
        return result.model_dump(exclude_none=False)

    async def increment_retry(state: RunState) -> RunState:
        model = await _load_model(store, state)
        model.retry_count += 1
        model.status = "validation_retry"
        await store.save_state(model)
        return model.model_dump(exclude_none=False)

    async def validate_mutation(state: RunState) -> RunState:
        model = await _load_model(store, state)
        result = await nodes.validate_mutation(model)
        return result.model_dump(exclude_none=False)

    async def validate_security(state: RunState) -> RunState:
        model = await _load_model(store, state)
        result = await nodes.validate_security(model)
        return result.model_dump(exclude_none=False)

    async def route_pr(state: RunState) -> RunState:
        from backend.orchestrator.trust_gating import apply_trust_gates_before_pr

        model = await _load_model(store, state)
        model = apply_trust_gates_before_pr(model, settings.max_retries)
        await store.save_state(model)
        result = await nodes.route_pr(model)
        return result.model_dump(exclude_none=False)

    graph.add_node("prepare_repo", prepare_repo)
    graph.add_node("environment_precheck", environment_precheck)
    graph.add_node("halt_environment", halt_environment)
    graph.add_node("index_repository", index_repository)
    graph.add_node("parallel_intel", parallel_intel)
    graph.add_node("layer1_fan_in", layer1_fan_in)
    graph.add_node("reproduction_gate", reproduction_gate)
    graph.add_node("investigate", investigate)
    graph.add_node("blast_scope", blast_scope)
    graph.add_node("engineer_context", engineer_context)
    graph.add_node("plan_fixes", plan_fixes)
    graph.add_node("generate_code", generate_code)
    graph.add_node("increment_retry", increment_retry)
    graph.add_node("validate_mutation", validate_mutation)
    graph.add_node("validate_security", validate_security)
    graph.add_node("route_pr", route_pr)

    graph.set_entry_point("prepare_repo")
    graph.add_edge("prepare_repo", "environment_precheck")
    graph.add_edge("environment_precheck", "index_repository")
    graph.add_edge("index_repository", "parallel_intel")
    graph.add_edge("parallel_intel", "layer1_fan_in")
    # The probe runs immediately after the clone, but the *gate* is here.
    # Static intelligence (A1/A2/A3) reads source and needs no installed
    # dependencies, so it still produces real findings on an unprepared
    # repository; what stops is everything that must execute code.
    graph.add_conditional_edges("layer1_fan_in", after_environment, {
        "reproduction_gate": "reproduction_gate",
        "halt_environment": "halt_environment",
    })
    graph.add_edge("halt_environment", END)
    graph.add_edge("reproduction_gate", "investigate")
    graph.add_conditional_edges("investigate", should_reinvestigate, {
        "investigate": "investigate",
        "blast_scope": "blast_scope",
    })
    graph.add_edge("blast_scope", "engineer_context")
    graph.add_edge("engineer_context", "plan_fixes")
    graph.add_edge("plan_fixes", "generate_code")
    graph.add_edge("generate_code", "validate_mutation")

    def mutation_router(state: RunState) -> str:
        result = after_mutation(state)
        if result == "generate_code":
            return "increment_retry"
        return result

    graph.add_conditional_edges("validate_mutation", mutation_router, {
        "validate_security": "validate_security",
        "increment_retry": "increment_retry",
        "route_pr": "route_pr",
    })
    graph.add_edge("increment_retry", "generate_code")

    def security_router(state: RunState) -> str:
        result = after_security(state)
        if result == "generate_code":
            return "increment_retry"
        return "route_pr"

    graph.add_conditional_edges("validate_security", security_router, {
        "route_pr": "route_pr",
        "increment_retry": "increment_retry",
    })
    graph.add_edge("route_pr", END)

    return graph


async def _load_model(store: RedisStore, state: RunState):
    from backend.state.schema import RunStateModel
    loaded = await store.load_state(state["run_id"])
    if loaded:
        for k, v in state.items():
            if v is not None and k in RunStateModel.model_fields:
                setattr(loaded, k, v)
        return loaded
    return RunStateModel(**{k: v for k, v in state.items() if k in RunStateModel.model_fields})
