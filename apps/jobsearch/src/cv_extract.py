"""
cv_extract.py
--------------
Convierte un CV en LaTeX (.tex) a texto plano legible, para que los prompts
que le mandamos a la IA no carguen ruido de comandos LaTeX (\\textbf, \\hfill,
\\item[leftmargin=...], etc). No pretende ser un compilador LaTeX completo:
es un heurístico dirigido a los comandos que aparecen en plantillas de CV
típicas (incluida la tuya). Si el archivo no es LaTeX (ya es texto plano),
se regresa sin tocar.
"""

import re


def _looks_like_latex(text: str) -> bool:
    return "\\documentclass" in text or "\\begin{document}" in text


def _strip_comments(text: str) -> str:
    # Elimina comentarios de LaTeX (% no escapado), preservando \%
    return re.sub(r'(?<!\\)%.*', '', text)


def _extract_document_body(text: str) -> str:
    match = re.search(r'\\begin\{document\}(.*)\\end\{document\}', text, re.DOTALL)
    return match.group(1) if match else text


def _unwrap_repeated(pattern: str, repl: str, text: str, max_iter: int = 6) -> str:
    """Aplica un regex repetidamente hasta que ya no haya cambios,
    necesario para comandos anidados como \\textbf{\\textit{x}}."""
    for _ in range(max_iter):
        new_text = re.sub(pattern, repl, text)
        if new_text == text:
            break
        text = new_text
    return text


def latex_to_text(raw: str) -> str:
    if not raw or not raw.strip():
        return ""

    if not _looks_like_latex(raw):
        return raw.strip()

    text = _strip_comments(raw)
    text = _extract_document_body(text)

    # \href{url}{texto} -> "texto (url)"
    text = _unwrap_repeated(r'\\href\{([^{}]*)\}\{([^{}]*)\}', r'\2 (\1)', text)

    # Encabezados de sección -> marcador legible
    text = re.sub(r'\\section\*?\{([^{}]*)\}', r'\n\n== \1 ==\n', text)

    # Separadores comunes en modo matemático simple: $\vert$, $\cdot$, etc.
    text = re.sub(r'\$\\vert\$', '|', text)
    text = re.sub(r'\$\\cdot\$', '·', text)

    # Comandos de énfasis que solo deben dejar su contenido
    for cmd in ['textbf', 'textit', 'emph', 'LARGE', 'Large', 'large', 'uppercase']:
        text = _unwrap_repeated(r'\\' + cmd + r'\{([^{}]*)\}', r'\1', text)

    # \item[opcional] -> viñeta de texto plano
    text = re.sub(r'\\item(\[[^\]]*\])?', '\n- ', text)

    # Saltos de línea explícitos de LaTeX
    text = text.replace('\\\\', '\n')
    text = text.replace('\\hfill', ' | ')

    # \begin{entorno}[opcional] / \end{entorno} -> fuera
    text = re.sub(r'\\begin\{[^{}]*\}(\[[^\]]*\])?', '', text)
    text = re.sub(r'\\end\{[^{}]*\}', '', text)

    # Cualquier comando restante con argumento simple: \cmd{arg} o \cmd[opt]{arg}
    text = _unwrap_repeated(r'\\[a-zA-Z]+(\[[^\]]*\])?\{[^{}]*\}', '', text)

    # Comandos sueltos sin argumento (\noindent, \vspace, \titlerule, etc.)
    text = re.sub(r'\\[a-zA-Z]+\*?', '', text)

    # Llaves residuales y espacios especiales de LaTeX
    text = text.replace('{', '').replace('}', '')
    text = text.replace('~', ' ')
    text = text.replace('\\', '')

    # Colapsar espacios y líneas en blanco excesivas
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n[ \t]+', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "/app/cv.txt"
    with open(path, encoding="utf-8") as f:
        print(latex_to_text(f.read()))
