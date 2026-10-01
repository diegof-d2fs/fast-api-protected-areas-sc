# Padrões de desenvolvimento

## Documentação de código

Comentários e docstrings devem explicar o motivo de uma restrição, regra de negócio ou solução
não evidente. Não devem repetir o código, narrar alterações históricas, registrar changelog,
mencionar prompts ou ferramentas de geração, atribuir autoria artificial ou humana, nem citar
número de fase, item ou requisito de um documento de planejamento (SDD, PRD, plano de
implementação). Um comentário deve continuar correto e útil para alguém que nunca viu esses
documentos e não sabe que este é um projeto de TCC — escrever como se o código fosse subir em
produção pela primeira vez, no nível de um tech lead revisando a base antes do lançamento.
Histórico, decisões arquiteturais e o vínculo com fases/requisitos pertencem ao Git, aos ADRs e à
documentação em `docs/`, nunca ao código-fonte em si.

Válido para os três repositórios do produto (`pipeline-protected-areas-sc`,
`fast-api-protected-areas-sc`, `frontend-protected-areas-sc`), não só para este.
