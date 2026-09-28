from src.utils.io import read_json,write_json
class ChapterGenerator:
    def __init__(self,gemini): self.gemini=gemini
def word_count(s): return len(s.split())
def generate_chapter(gemini,path,number,title,context,target_words):
    existing=read_json(path)
    if existing and existing.get('status')=='completed' and word_count(existing.get('script',''))>100:return existing
    p=f"""Write chapter {number}: {title} for an ORIGINAL educational audiobook. Target about {target_words} words. Research/context: {context.get('research',{})}. Previous continuity: {context.get('previous_summary','')}. Write natural narration, no citations read aloud, no copied passages. Return JSON: title, script, summary, important_concepts, unresolved_ideas, terminology, transition, word_count."""
    d=gemini.json(p); d.update(chapter_number=number,status='completed',word_count=word_count(d['script'])); write_json(path,d); return d
