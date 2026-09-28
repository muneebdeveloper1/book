def plan(gemini,script,count=15):
    return gemini.json(f'''Create {count} contextual visual scenes for this ORIGINAL audiobook script. Return JSON array with scene_id, chapter, description, visual_type, prompt, source_type. Do not suggest copyrighted media. Script excerpt/package: {script[:30000]}''')
