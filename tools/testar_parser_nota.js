// Testa o nó Code "Separa Nota/Feedback" do workflow-n8n.v2.json, sem precisar do n8n.
//
//   node tools/testar_parser_nota.js
//
// Extrai o jsCode direto do JSON gerado, então o teste nunca fica dessincronizado
// do workflow de verdade.

const fs = require('fs');
const path = require('path');

const caminhoWorkflow = path.join(__dirname, '..', 'workflow-n8n.v2.json');
const workflow = JSON.parse(fs.readFileSync(caminhoWorkflow, 'utf8'));
const no = workflow.nodes.find((n) => n.name === 'Separa Nota/Feedback');
if (!no) {
  console.error('Nó "Separa Nota/Feedback" não encontrado no workflow v2.');
  process.exit(1);
}

const parser = new Function('$input', `${no.parameters.jsCode}\n//# sourceURL=parser.js`);

const casos = [
  {
    rotulo: 'JSON limpo',
    texto: '{"nota": 8.5, "feedback": "Bom trabalho."}',
    esperado: { nota: 8.5, feedback: 'Bom trabalho.' },
  },
  {
    rotulo: 'com cercas de markdown e aspas escapadas',
    texto: '```json\n{"nota": 7, "feedback": "Usa \\"aspas\\" aqui"}\n```',
    esperado: { nota: 7, feedback: 'Usa "aspas" aqui' },
  },
  {
    rotulo: 'frase antes do JSON e nota como texto com vírgula',
    texto: 'Segue:\n{"nota": "9,5", "feedback": "ok"}',
    esperado: { nota: 9.5, feedback: 'ok' },
  },
  {
    rotulo: 'nota fora da faixa é limitada',
    texto: '{"nota": 13.4, "feedback": "exagero"}',
    esperado: { nota: 10, feedback: 'exagero' },
  },
  {
    rotulo: 'sem feedback textual ainda é aceito',
    texto: '{"nota": 6, "feedback": ""}',
    esperado: { nota: 6, feedback: '(o avaliador não retornou feedback textual)' },
  },
  {
    rotulo: 'sem nota deve lançar erro (o workflow marca FALHOU)',
    texto: '{"feedback": "sem nota"}',
    deveLancar: true,
  },
  {
    rotulo: 'resposta sem JSON deve lançar erro',
    texto: 'Não consigo avaliar esta entrega.',
    deveLancar: true,
  },
  {
    rotulo: 'resposta sem parts deve lançar erro',
    payload: { content: {} },
    deveLancar: true,
  },
];

let falhas = 0;
for (const caso of casos) {
  const payload = caso.payload ?? { content: { parts: [{ text: caso.texto }] } };
  let resultado;
  let erro = null;
  try {
    resultado = parser({ item: { json: payload } });
  } catch (e) {
    erro = e;
  }

  if (caso.deveLancar) {
    if (erro) {
      console.log(`ok       ${caso.rotulo}`);
    } else {
      falhas++;
      console.log(`FALHOU   ${caso.rotulo} — deveria lançar, devolveu ${JSON.stringify(resultado)}`);
    }
    continue;
  }

  if (erro) {
    falhas++;
    console.log(`FALHOU   ${caso.rotulo} — lançou: ${erro.message}`);
    continue;
  }

  const igual = resultado.json.nota === caso.esperado.nota
    && resultado.json.feedback === caso.esperado.feedback;
  if (igual) {
    console.log(`ok       ${caso.rotulo}`);
  } else {
    falhas++;
    console.log(`FALHOU   ${caso.rotulo}\n         esperado ${JSON.stringify(caso.esperado)}`
      + `\n         obtido   ${JSON.stringify(resultado.json)}`);
  }
}

console.log(falhas === 0 ? '\nTodos os casos passaram.' : `\n${falhas} caso(s) falharam.`);
process.exit(falhas === 0 ? 0 : 1);
