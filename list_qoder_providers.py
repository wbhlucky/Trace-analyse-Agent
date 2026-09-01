import asyncio, json, os
from pathlib import Path

os.chdir(r'd:\Users\Administrator\Desktop\DitingAgent-main')

async def main():
    from qoder_agent_sdk import QoderAgentOptions, QoderSDKClient, access_token_from_env, qodercli_auth
    # Prefer CLI login; fall back to PAT if needed is handled by SDK.
    # Use qodercli_auth to reuse the interactive login you just completed.
    options = QoderAgentOptions(
        model=None,
        system_prompt="",
        cwd=os.getcwd(),
        auth=qodercli_auth(),
        permission_mode="dontAsk",
    )
    async with QoderSDKClient(options=options) as client:
        providers = await client.list_byok_providers()
        if not providers:
            print("No BYOK providers returned (可能是认证或账号权限问题)")
            return
        for p in providers:
            print("=" * 70)
            print("provider:", p.get("key"))
            print("  display:", p.get("display_name"))
            print("  api_key_url:", p.get("api_key_url"))
            for t in p.get("types") or []:
                print(f"  type: {t.get('key')} ({t.get('display_name')})")
                for m in t.get("models") or []:
                    print(f"    - {m.get('key')}  ({m.get('display_name')})  format={m.get('format')}")

asyncio.run(main())
