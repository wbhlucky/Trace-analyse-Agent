import asyncio, os, sys
sys.path.insert(0, 'src'); os.chdir('d:/Users/Administrator/Desktop/DitingAgent-main')
os.environ.pop('QODER_PERSONAL_ACCESS_TOKEN', None)
V = [
  ('bailian-deepseek-tp', {'provider':'bailian','model':'deepseek-v4-pro-tp','api_key':'sk-sp-H.DLDRPE.g1du.MEUCIQDCTtQZ93yvCH7ctLOyRyAvuT8-CoGwJG3baZ4Io8N3RwIgDxLL7hhGE1nmApEZX-zzcF_W0dmuDjeiRm_Xv0teyfw'}),
  ('bailian-deepseek-pg', {'provider':'bailian','model':'deepseek-v4-pro-pg','api_key':'sk-sp-H.DLDRPE.g1du.MEUCIQDCTtQZ93yvCH7ctLOyRyAvuT8-CoGwJG3baZ4Io8N3RwIgDxLL7hhGE1nmApEZX-zzcF_W0dmuDjeiRm_Xv0teyfw'}),
]
async def run(name, cm):
    from qoder_agent_sdk import QoderAgentOptions, QoderSDKClient, qodercli_auth, ResultMessage, AssistantMessage, TextBlock
    opt = QoderAgentOptions(model=None, system_prompt='Test.', cwd=os.getcwd(), auth=qodercli_auth(), resolve_model=lambda ctx: {'model': cm}, permission_mode='dontAsk', max_turns=1)
    async with QoderSDKClient(options=opt) as cl:
        await cl.query('Reply OK')
        async for m in cl.receive_response():
            if isinstance(m, AssistantMessage): return [b.text for b in m.content if isinstance(b, TextBlock)]
            if isinstance(m, ResultMessage): return 'RESULT:' + str(getattr(m,'errors',None))
async def main():
    for n, cm in V:
        print(n, '=>', await run(n, cm))
asyncio.run(main())
