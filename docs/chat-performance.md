# Desempenho do assistente — 5 e 6 de outubro de 2026

O chat mantém Jev e Mercury, perguntas sobre páginas, aprovação de compromissos externos e
verificação independente. Foram removidas duas chamadas auxiliares por tarefa comum; geração
de texto e avaliação de segurança se sobrepõem. Capturas não fazem parte do caminho de execução.

## Comparação controlada da primeira entrega

Esta comparação registra a primeira entrega, anterior ao ajuste de navegação descrito abaixo.
Os hashes e todas as tentativas daquela versão permanecem nos arquivos de evidência.

Chrome real, fixture HTTP local, três pares alternados antes/depois por cenário. Os modelos
foram simulados: 80 ms por auxiliar, 100 ms para texto e 15 ms por decisão. A captura lenta
acrescenta 250 ms por chamada de screenshot. **Nenhuma API paga foi chamada.**

O cronômetro começa antes de enviar a mensagem e termina após o trabalhador concluir a resposta.
Inclui abertura da aba, navegação, modelos simulados, guardas e verificação. A publicação recebe
aprovação automática somente neste teste local; a aplicação continua exigindo confirmação humana.
Fechamento da aba e a leitura independente adicional do resultado ficam fora do cronômetro.

| Cenário | Mediana antes | Mediana depois | Redução |
| --- | ---: | ---: | ---: |
| Busca com preenchimento | 2.186 s | 1.058 s | 51,6% |
| Navegação para artigo local | 1.011 s | 0.539 s | 46,7% |
| Pergunta sobre a página | 0.475 s | 0.344 s | 27,7% |
| Publicação com confirmação | 1.026 s | 0.568 s | 44,6% |
| Atualização durante DONE | 1.791 s | 0.814 s | 54,5% |
| Captura lenta | 2.849 s | 0.882 s | 69,1% |

**36/36 execuções** passaram pela conferência do chat e pela leitura independente do DOM.
Todos os três pares de cada cenário estão incluídos, inclusive buscas de 4.869 s antes e 2.280 s depois.
O controle dinâmico muda durante a decisão terminal e depois estabiliza: não demonstra sucesso
em páginas cujo conteúdo continua mudando durante a verificação final.

| Cenário | Auxiliares antes → depois | Decisões antes → depois | Geração de texto |
| --- | ---: | ---: | ---: |
| Busca / captura lenta | 6 → 4 | 3 → 3 | 1 |
| Navegação / publicação | 5 → 3 | 2 → 2 | 0 |
| Pergunta | 2 → 2 | 0 → 0 | 0 |
| Atualização durante DONE | 6 → 4 | 4 → 3 | 1 |

Uma comparação adicional com navegador simulado também passou **36/36**. Suas reduções de
mediana foram 45,7% na busca, 46,9% na navegação, 22,2% na pergunta, 46,9% na publicação,
47,4% no caso dinâmico e 77,9% com captura lenta. Estes são testes do fluxo local; não medem
as latências reais, precisão ou disponibilidade de Jev/Mercury em sites externos.

## Evidência e reprodução

- `artifacts/chat-speed-real-browser.json`: comparação em Chrome, tentativas, contagens,
  observações independentes, tempos por etapa e hashes do código.
- `artifacts/chat-speed-simulated.json`: comparação com navegador e modelos simulados.
- `artifacts/performance-baseline/jev_ultrafast/`: cópia congelada do código anterior às mudanças.
- O comparador verifica que o código permaneceu estável durante cada comparação e conserva
  relatórios posteriores em `artifacts/chat-speed-runs/`. Esses arquivos locais são ignorados pelo Git.

```powershell
uv run python scripts/benchmark_chat.py
uv run python scripts/benchmark_chat.py --real-browser
```

O segundo comando requer Chrome/CDP já conectado pelo inicializador Windows. Ambos bloqueiam
o transporte de modelos reais. Uma instalação nova precisa da cópia anterior congelada para
comparar; o script informa sua ausência em vez de fabricar uma linha de base.

## Validação funcional

- 146 testes offline passaram: chamadas combinadas, geração em paralelo sem input prematuro,
  captura bloqueada sem impedir conclusão, cache de texto, evidência falsa, confirmação,
  pausa, redimensionamento, DONE obsoleto e ausência de repetição de mutações incertas.
- 26 guardas no Chrome local passaram, incluindo geometria das marcações e execução única.
- Ruff, verificações JavaScript, build/lint do frontend e `uv build` passaram; são checks de entrega;
  seus resultados não são evidência de desempenho de provedores.
- Na interface real com modelos simulados: busca com duas ações e quatro auxiliares,
  pergunta com zero ações/geração de texto, publicação executada uma vez após confirmação,
  exportação completa sem screenshot, histórico e prévia móvel validados.
- Na interface estática: busca, prévia, inspeção, exportação e pergunta com zero ações validadas.
- A captura móvel mediu 389 × 687 sem transbordamento. A diferença máxima entre as marcações
  e a geometria da captura foi 0,094 px no desktop. Evidências locais: `chat-speed-ui-export.json`,
  `chat-speed-desktop.png`, `chat-speed-mobile.png`, sob `artifacts/`.

## Ajuste adicional de navegação

A navegação aguardava `document.readyState === "complete"`, incluindo imagens secundárias.
Agora aguarda DOMContentLoaded, incluindo scripts `defer`, e permite observar os controles enquanto
essas imagens continuam carregando. Conteúdo assíncrono de uma aplicação ainda pode precisar de
outra observação; guardas de alvo, confirmação e evidência final continuam obrigatórios.

No Chrome real, com os mesmos modelos simulados, um script `defer` atrasado em 200 ms e uma
imagem atrasada em 2 s, três pares alternados produziram:

| Antes | Depois | Redução mediana | Resultados independentes |
| ---: | ---: | ---: | ---: |
| 2.729 s | 0.792 s | 71,0% | 6/6 |

Nas três execuções novas, o script já estava inicializado, a imagem ainda estava carregando e
o resultado do artigo foi confirmado no DOM. Cada execução usou três auxiliares, duas decisões,
zero gerações de texto e exatamente uma ação executada. As seis tentativas estão incluídas;
não houve chamadas pagas nem repetição de ações.

`GET /api/preview?after=...` agora descarta a imagem inalterada antes de copiar seus elementos.
O estado compacto lê os totais de tempo sem percorrer os eventos detalhados. Os testes cobrem
essas duas operações e a navegação sem repetir uma solicitação com erro.

Evidência: `artifacts/navigation-speed-real-browser.json`, com hashes estáveis e observação final.
A linha de base é a primeira entrega congelada em `artifacts/navigation-baseline/jev_ultrafast/`.

```powershell
uv run python scripts/benchmark_chat.py --real-browser --case slow_resource --baseline artifacts/navigation-baseline --output artifacts/navigation-speed-real-browser.json
```

O registro existente da busca na Amazon, lido sem repetir a tarefa, mediu 22.334 ms no total
e 6.781 ms de navegação. O servidor descartou a conclusão porque a página mudou durante a
verificação. Isso identifica uma espera real, mas não constitui uma comparação antes/depois
em site externo. O registro local está em `artifacts/navigation-live-before.json`.

## Limites

### Controle manual: validação de 6 de outubro

O executor humano usa a mesma aba CDP, com propriedade exclusiva, sem Jev ou Mercury durante
a intervenção. A prévia mantém uma captura em andamento e mínimo de 200 ms no manual;
o mínimo automático continua em 500 ms. O teste com captura bloqueada confirma que a entrada
não aguarda a imagem e que uma captura de viewport antigo não é publicada.

Uma execução local pela interface React, com modelos simulados e esperas deliberadas do teste,
registrou 20.967 ms desde o pedido até a conclusão: 16.885 ms de controle manual, 1.400 ms de
confirmação e 2.681 ms restantes no contador automático. A diferença de 1 ms decorre do
arredondamento. Esses números não medem a velocidade de um usuário ou provedor real.
Assumir uma tarefa concluída posteriormente preserva seus tempos anteriores.

Foram três auxiliares (`request`, `safety`, `verify`) e três decisões (bloqueio, publicação,
conclusão). A publicação ocorreu uma única vez, depois da confirmação; nenhuma chamada de modelo
ocorreu durante as entradas humanas. A falha simulada após um evento foi registrada como incerta,
sem repetição, e exigiu recuperação explícita. A evidência resumida fica em
`artifacts/manual-control-validation.json`, sem valores digitados ou imagens protegidas.

Validação concluída: 168 testes pytest, Ruff, verificações JavaScript, build/lint do frontend,
26 guardas de navegador, seis grupos de verificações reais de controle manual e `uv build`.
O pacote inclui executor, proteção e módulo de entrada da interface. React foi validado com
login, OTP com avanço automático, colagem, composição, teclado, rolagem, duplo clique, iframe de
outra origem e arrasto em canvas; a interface estática confirmou coordenadas escalonadas,
digitação, recarga e saída mantendo a tarefa pausada. Duas interfaces, confirmação invalidada,
navegação, resize, captura atrasada, deduplicação, expiração e reinício também têm regressões.
Os valores sintéticos não apareceram nos pedidos de modelos, estados, histórico ou exportações.

CAPTCHAs são simulados e resolvidos pela interação humana no teste. A compatibilidade com
serviços reais depende do site. Uploads, downloads, novas abas e diálogos nativos ficam fora
do escopo. Não houve chamadas pagas, alteração de provedores/configuração, commit ou push.

A execução externa anteriormente registrada acumulou 112.924 ms no contador do agente e
terminou pausada. Suas oito decisões duraram 507–734 ms; os auxiliares somaram 19.729 ms.
Não havia instrumentação suficiente para atribuir todo o restante a capturas, navegador ou
outra etapa. Os novos registros permitem separar esses tempos sem presumir sua causa.

A marca original de 7,073 s em Google Flights continua sendo do ciclo da biblioteca, com
limites de medição diferentes. Esta mudança não demonstra aquele tempo no chat nem garante
uma redução percentual em páginas ou provedores externos. Imagens são opcionais e podem ser
descartadas quando conteúdo ou geometria mudam; nenhuma imagem alimenta os modelos.
