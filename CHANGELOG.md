# Changelog

## 2026-06-18

Fix mayor del case multi-provider con Anthropic como modelo principal (usando
suscripción/OAuth, no API key). El flujo "Anthropic main + slot externo
(MiniMax / OpenCode Go / Codex)" estaba mostrando errores intermitentes de
API: a veces conectaba, a veces 401, a veces 400. Se identificaron y
arreglaron nueve bugs acumulados en la frontera UI ↔ `claude-multi` ↔
`smart-proxy.py`. Además se incorporó un test environment (`test-multi.sh`)
para reproducir cada escenario.

### Fixes

- **A — UI (`claude-harness-ui.py`)**: el `provider_id` `"claude"` (Anthropic
  nativo) se exportaba como `CLAUDE_HARNESS_MAIN_BACKEND="claude"`, valor
  que el proxy no reconoce. Ahora se mapea a `"anthropic"`.
- **B — UI**: normalización del prefijo `claude/` antes del check
  `is_standard`, para que modelos como `claude/opus` no activen el "señuelo"
  innecesariamente.
- **C — proxy (`smart-proxy.py`)**: re-incorporado `"claude/": "anthropic"`
  al `prefix_map` de `detect_backend`. Sin esto, `claude/opus` caía al
  fallback `"main"` y nunca llegaba al alias resolution.
- **D — proxy**: simplificación de la rama Anthropic. Path A
  (passthrough de la auth del cliente) es ahora la ruta primaria, ya que
  Claude Code refresca el OAuth internamente; Path B (`load_anthropic_auth`)
  queda solo como fallback cuando el cliente envía la dummy
  `smart-proxy-passthrough`.
- **E — `claude-multi`**: en el branch `auth_ok anthropic` ahora también se
  hace `unset ANTHROPIC_AUTH_TOKEN`, para impedir que un token viejo del
  shell shadow la suscripción OAuth. El branch `else` setea el dummy de
  forma dura (sin parameter expansion) por la misma razón.
- **F — `claude-multi`**: defensa en profundidad — el case `claude-*` /
  `claude/*` / `anthropic/*` setea `MAIN_BACKEND="anthropic"`, y se
  normaliza `"claude" → "anthropic"` post-export por si la UI dejó pasar
  un valor incorrecto.
- **G — proxy**: `_build_anthropic_request` usaba
  `Cookie: sessionKey=<token>` para OAuth, formato que solo funciona contra
  `claude.ai`. Cambiado a `Authorization: Bearer <token>`, que es el
  esperado por `api.anthropic.com/v1/messages`.
- **H — proxy**: alias `opus`/`sonnet`/`haiku`/`fable` apuntaban a snapshots
  descontinuados (`claude-3-opus-20240229` → 404 not_found). Actualizados
  a `claude-opus-4-8`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`,
  `claude-fable-5`. Para reducir la fragilidad a futuro, la UI/`claude-multi`
  ahora pasan alias limpios (sin prefijo `claude/`) y Claude Code los
  resuelve contra su catálogo interno (que ships actualizado en cada
  release).
- **I — proxy**: `load_anthropic_auth` ahora prioriza OAuth de
  `~/.claude/.credentials.json` por sobre `ANTHROPIC_AUTH_TOKEN`. Antes era
  al revés, lo que silenciosamente facturaba contra una API key cuando el
  usuario quería usar su suscripción. También ignora la dummy
  `smart-proxy-passthrough` si la encuentra en la env.
- **J — UI + `claude-multi`**: strip del prefijo `claude/` antes de exportar
  `ANTHROPIC_MODEL` / `ANTHROPIC_DEFAULT_*_MODEL`. Garantiza que Claude
  Code reciba el alias limpio (`opus`, `sonnet`…) y lo resuelva contra su
  catálogo interno.

### Nuevo

- `scripts/test-multi.sh`: test environment con 6 escenarios end-to-end
  (`anthropic-puro`, `opus+minimax-slots`, `opus+opencodego-slots`,
  `multi-claude-opus`, `multi-anthropic-opus`, `multi-mixed`). Mata
  cualquier proxy previo antes de cada caso, captura stderr a
  `/tmp/test-multi/<caso>/smart-proxy.log` y reporta PASS/FAIL.

### Notas operativas

- El proxy es un proceso Python que se queda residente en `127.0.0.1:8081`.
  Cuando se editan los scripts hay que matarlo para que la próxima
  invocación de `claude-multi` lo relance con el código nuevo:
  `lsof -t -iTCP:8081 -sTCP:LISTEN | xargs -r kill -9`. `test-multi.sh` ya
  lo hace en cada caso.
- `install.sh` baja los scripts desde el remote en GitHub. Si tenés cambios
  locales no pusheados, re-correr el installer los pisa. Mantener la edición
  contra el repo y propagar con `git pull` + reinstall, o trabajar
  directo en `~/.local/share/claude-harness/scripts/` durante el debugging.
