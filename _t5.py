import asyncio, os, sys
sys.path.insert(0, 'src'); os.chdir('d:/Users/Administrator/Desktop/DitingAgent-main')

async def run(name, cm):
    from qoder_agent_sdk import QoderAgentOptions, QoderSDKClient, access_token_from_env, ResultMessage, AssistantMessage, TextBlock
    from trace_agent.config import LlmRuntimeConfig
    from pathlib import Path
    c = LlmRuntimeConfig.resolve(project_root=Path('.'))
    opt = QoderAgentOptions(model=None, system_prompt='Test.', cwd=os.getcwd(), env=dict(c.sdk_environment), auth=access_token_from_env(), resolve_model=lambda ctx: {'model': cm}, permission_mode='dontAsk', max_turns=1)
    try:
        async with asyncio.timeout(40):
            async with QoderSDKClient(options=opt) as cl:
                await cl.query('Reply OK')
                async for m in cl.receive_response():
                    if isinstance(m, AssistantMessage): return [b.text for b in m.content if isinstance(b, TextBlock)]
                    if isinstance(m, ResultMessage): return 'RESULT:' + str(getattr(m,'errors',None))
    except Exception as e:
        return 'EXC:' + str(e)[:250]

async def main():
    key='sk-sp-H.DLDRPE.g1du.MEUCIQDCTtQZ93yvCH7ctLOyRyAvuT8-CoGwJG3baZ4Io8N3RwIgDxLL7hhGE1nmApEZX-zzcF_W0dmuDjeiRm_Xv0teyfw'
    for n, cm in [
        ('bailian-tp', {'provider':'bailian','model':'deepseek-v4-pro-tp','api_key':key}),
        ('bailian-pg', {'provider':'bailian','model':'deepseek-v4-pro-pg','api_key':key}),
    ]:
        print(n, '=>', await run(n, cm))

asyncio.run(main())
