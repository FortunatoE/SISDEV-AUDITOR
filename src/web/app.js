const $ = (id) => document.getElementById(id);

const STORAGE_KEY = 'sisdev-auditor.import-jobs.v2';
// Multipart uploads pass through a Vercel Function. Keep the client-side
// validation aligned with the backend limit so a large file fails clearly.
const MAX_UPLOAD_SIZE = 4 * 1024 * 1024;
const TERMINAL_STATUSES = new Set(['COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED']);
const SUCCESS_STATUSES = new Set(['COMPLETED', 'COMPLETED_WITH_WARNINGS']);

const UPLOAD_SOURCES = [
  { id: 'sap_entry_current', title: 'SAP — Entradas', expected: 'entrada_sisdev.xlsx', accept: ['.xlsx', '.xls'], detail: 'Notas de venda com retorno para a unidade comercial.' },
  { id: 'sap_exit_current', title: 'SAP — Saídas', expected: 'saída_sisdev.xlsx', accept: ['.xlsx', '.xls'], detail: 'Transferências para a unidade produtora.' },
  { id: 'sap_entry_history', title: 'SAP — Entrada (histórico)', expected: 'entrada_sisdev.xlsx', accept: ['.xlsx', '.xls'], detail: 'Histórico de entradas SAP.' },
  { id: 'sap_exit_history', title: 'SAP — Saída (histórico)', expected: 'saída_sisdev.xlsx', accept: ['.xlsx', '.xls'], detail: 'Histórico de saídas SAP.' },
  { id: 'sap_stock', title: 'SAP — Estoque', expected: 'MB52.xlsx', accept: ['.xlsx', '.xls'], detail: 'Saldo por centro, material e lote.' },
  { id: 'sisdev_stock', title: 'SISDEV — Estoque', expected: 'Relatório Saldo de Agrotóxico.pdf', accept: ['.pdf'], detail: 'Saldo de estoque exportado do SISDEV em PDF.' },
  { id: 'sisdev_movement', title: 'SISDEV — Movimentações', expected: 'Relatório de análise de movimentação.xlsx', accept: ['.xlsx', '.xls'], detail: 'Movimentos de entrada e saída do SISDEV.' },
  { id: 'agrotis_recipe', title: 'Agrotis — Receitas', expected: 'ReceitasEmitidas.xls', accept: ['.xlsx', '.xls'], detail: 'Receitas emitidas e dados do responsável técnico.' },
];

const PAGE_CONFIG = {
  pending: { title: 'Pendências', description: 'Análise lote a lote dos documentos que exigem conferência.', empty: 'Nenhuma pendência encontrada.', columns: ['status', 'diagnosis', 'confidence', 'nf', 'series', 'doc_date', 'center', 'direcao', 'sap_material', 'lote_sap', 'lote_fabricante', 'quantidade_sap', 'unidade_sap', 'produto_sisdev', 'lote_sisdev', 'quantidade_sisdev'] },
  regularization: { title: 'Regularizar SISDEV', description: 'Fila operacional com os dados necessários para regularização no SISDEV.', empty: 'Nenhum documento para regularizar.', columns: ['direcao', 'situacao', 'data_documento', 'cnpj', 'numero_nfe', 'serie', 'produto', 'lote', 'quantidade', 'volume_embalagem', 'quantidade_embalagem', 'numero_receituario', 'art', 'nome_rt', 'cultura', 'diagnostico', 'ure', 'dose_recomendada', 'area'] },
  analysis: { title: 'Análises', description: 'Resumo das ocorrências por status, diagnóstico e confiança.', empty: 'Nenhuma análise disponível.', columns: ['status', 'diagnosis', 'confidence', 'ocorrencias'] },
  invoices: { title: 'Notas Fiscais', description: 'Notas fiscais SAP consolidadas por documento, centro e direção.', empty: 'Nenhuma nota fiscal encontrada.', columns: ['nf', 'series', 'doc_date', 'center', 'direcao', 'linhas', 'quantidade_sap'] },
  recipes: { title: 'Receitas', description: 'Receitas Agrotis por emissão, produto, responsável técnico e propriedade.', empty: 'Nenhuma receita encontrada.', columns: ['data_emissao', 'numero_receita', 'produto', 'volume_receita', 'unidade', 'emitido_por', 'art', 'diagnostico', 'nome_propriedade', 'cnpj'] },
  movements: { title: 'Movimentações', description: 'Movimentações SISDEV identificadas como entrada ou saída.', empty: 'Nenhuma movimentação encontrada.', columns: ['nf', 'series', 'direcao', 'movement_date', 'product', 'lot', 'embalagens', 'volume_embalagem', 'quantidade_total', 'unit', 'status'] },
  stocks: { title: 'Estoques', description: 'Comparação de quantidade SAP e quantidade SISDEV por centro, material e lote.', empty: 'Nenhum saldo de estoque encontrado.', columns: ['centro', 'deposito', 'material', 'lote', 'lote_fabricante', 'quantidade_sap', 'quantidade_sisdev', 'diferenca_sisdev_menos_sap', 'unidade'] },
  materials: { title: 'Materiais', description: 'Materiais SAP e suas chaves normalizadas para conciliação.', empty: 'Nenhum material encontrado.', columns: ['material_sap', 'normalizado', 'ocorrencias'] },
  lots: { title: 'Lotes', description: 'Comparação entre lote SAP e lote do fabricante.', empty: 'Nenhum lote encontrado.', columns: ['material', 'lote_sap', 'lote_fabricante', 'ocorrencias'] },
  units: { title: 'Unidades', description: 'Centros e CNPJs identificados nas fontes importadas.', empty: 'Nenhuma unidade encontrada.', columns: ['centro', 'cnpj', 'ocorrencias'] },
  movement_types: { title: 'Tipos de Movimento', description: 'Direções e tipos de movimento utilizados na auditoria.', empty: 'Nenhum tipo de movimento encontrado.', columns: ['codigo', 'tipo', 'ocorrencias'] },
  units_measure: { title: 'Unidades de Medida', description: 'Unidades de medida encontradas nas movimentações.', empty: 'Nenhuma unidade de medida encontrada.', columns: ['unidade', 'ocorrencias'] },
  rules: { title: 'Regras de Conciliação', description: 'Premissas ativas para datas, quantidades, lotes e estoques.', empty: 'Nenhuma regra cadastrada.', columns: ['indicador', 'valor'] },
  reports: { title: 'Relatórios', description: 'Validação consolidada pronta para exportação em CSV ou Excel.', empty: 'Nenhum registro disponível para relatório.' },
  history: { title: 'Histórico', description: 'Fontes e quantidades de linhas utilizadas na execução atual.', empty: 'Nenhum histórico disponível.', columns: ['source', 'source_file', 'linhas'] },
  logs: { title: 'Logs', description: 'Execuções de importação com início, término e resultado.', empty: 'Nenhuma execução registrada.', columns: ['id', 'started_at', 'finished_at', 'status', 'summary_json'] },
};

const LABELS = {
  diagnosis: 'Diagnóstico', confidence: 'Confiança', nf: 'NF', series: 'Série', doc_date: 'Data do documento',
  center: 'Centro', direcao: 'Direção', sap_material: 'Material SAP', lote_sap: 'Lote SAP',
  lote_fabricante: 'Lote fabricante', quantidade_sap: 'Quantidade SAP', unidade_sap: 'Unidade SAP',
  produto_sisdev: 'Produto SISDEV', lote_sisdev: 'Lote SISDEV', quantidade_sisdev: 'Quantidade SISDEV',
  diferenca_sisdev_menos_sap: 'Diferença', material_sap: 'Material SAP', normalizado: 'Normalizado',
  movement_date: 'Data do movimento', product: 'Produto', lot: 'Lote', unit: 'Unidade',
  source: 'Fonte', source_file: 'Arquivo', started_at: 'Início', finished_at: 'Término', summary_json: 'Resumo',
  numero_nfe: 'Número da NF-e', data_documento: 'Data do documento', numero_receituario: 'Número do receituário',
  nome_rt: 'Nome RT', dose_recomendada: 'Dose recomendada', ure: 'URE', art: 'ART', cnpj: 'CNPJ',
};

const state = {
  current: 'dashboard',
  optionsLoaded: false,
  jobs: loadStoredJobs(),
  pollTimers: new Map(),
  requestSequence: 0,
  reconciling: false,
  reconciliation: null,
  reconciliationPollTimer: 0,
};

class ApiError extends Error {
  constructor(message, status = 0, code = '') {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

function create(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(options)) {
    if (key === 'className') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'attrs') for (const [name, attrValue] of Object.entries(value)) node.setAttribute(name, attrValue);
    else node[key] = value;
  }
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) if (child !== null && child !== undefined) node.append(child);
  return node;
}

function asNumber(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function formatNumber(value) {
  return asNumber(value).toLocaleString('pt-BR', { maximumFractionDigits: 3 });
}

function formatLabel(value) {
  const key = String(value ?? '');
  if (LABELS[key]) return LABELS[key];
  return key.replaceAll('_', ' ').replace(/(^|\s)\S/g, (char) => char.toUpperCase());
}

function safeText(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'object') {
    try { return JSON.stringify(value); } catch { return '—'; }
  }
  return String(value);
}

function qs() {
  return new URLSearchParams({
    from: $('from').value,
    to: $('to').value,
    center: $('center').value,
    direction: $('direction').value,
    preferred_rt: $('rt-preference')?.value || '',
  });
}

function friendlyServerMessage(status, detail = '', raw = '') {
  const source = String(detail || raw || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  if (status === 401 || status === 403) return 'Sua sessão não tem permissão para executar esta ação.';
  if (status === 404) return 'O recurso solicitado ainda não está disponível no servidor.';
  if (status === 405) return 'Esta ação ainda não está habilitada no servidor.';
  if (status === 408 || status === 504 || /timed?\s*out|timeout/i.test(source)) return 'O servidor excedeu o tempo de resposta. O processamento continuará em segundo plano quando possível.';
  if (status === 413) return 'O arquivo é maior que o limite aceito pelo servidor.';
  if (status >= 500 || /traceback|sql|transaction|column\s+.+does not exist|internal server/i.test(source)) return 'O servidor encontrou um erro ao concluir a operação. Consulte o status da fonte e tente novamente.';
  if (source && source.length <= 240) return source;
  return 'Não foi possível concluir a operação.';
}

async function requestJson(url, options = {}, timeoutMs = 45_000) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(url, { ...options, signal: controller.signal });
  } catch (error) {
    if (error.name === 'AbortError') throw new ApiError('O servidor excedeu o tempo de resposta. O processamento pode continuar em segundo plano.', 0, 'TIMEOUT');
    throw new ApiError('Não foi possível conectar ao servidor. Verifique sua internet e tente novamente.', 0, 'NETWORK');
  } finally {
    window.clearTimeout(timer);
  }

  const raw = await response.text();
  let data = {};
  if (raw) {
    try { data = JSON.parse(raw); }
    catch {
      if (!response.ok) throw new ApiError(friendlyServerMessage(response.status, '', raw), response.status, 'NON_JSON');
      throw new ApiError('O servidor respondeu em um formato inesperado.', response.status, 'NON_JSON');
    }
  }
  if (!response.ok) throw new ApiError(friendlyServerMessage(response.status, data.error || data.message, raw), response.status, data.code || 'HTTP_ERROR');
  return data;
}

function loadStoredJobs() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function persistJobs() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state.jobs)); } catch { /* armazenamento pode estar bloqueado */ }
}

function normalizedBatchId(value) {
  return value === null || value === undefined || value === '' ? '' : String(value);
}

function resetJobsForBatch() {
  for (const timer of state.pollTimers.values()) window.clearTimeout(timer);
  state.pollTimers.clear();
  window.clearTimeout(state.reconciliationPollTimer);
  state.reconciliationPollTimer = 0;
  state.reconciliation = null;
  state.reconciling = false;
  state.jobs = {};
  persistJobs();
  for (const source of UPLOAD_SOURCES) renderJob(source.id);
  renderUploadSummary();
}

function keepOnlyBatch(batchId) {
  const expected = normalizedBatchId(batchId);
  if (!expected) return;
  const stored = Object.values(state.jobs).filter(Boolean);
  if (stored.some((job) => normalizedBatchId(job.batch_id) !== expected)) resetJobsForBatch();
}

function normalizeStatus(value, job = {}) {
  const raw = String(value || 'QUEUED').trim().toUpperCase().replaceAll(' ', '_');
  if (['PENDING', 'WAITING', 'AGUARDANDO', 'QUEUED'].includes(raw)) return 'QUEUED';
  if (['RUNNING', 'IN_PROGRESS', 'PROCESSANDO', 'PROCESSING'].includes(raw)) return 'PROCESSING';
  if (['DONE', 'SUCCESS', 'SUCCEEDED', 'CONCLUIDO', 'CONCLUÍDO', 'COMPLETED'].includes(raw)) {
    const warnings = asNumber(job.warning_count) || (Array.isArray(job.warnings) ? job.warnings.length : 0);
    return warnings ? 'COMPLETED_WITH_WARNINGS' : 'COMPLETED';
  }
  if (['COMPLETED_WITH_WARNINGS', 'SUCCESS_WITH_WARNINGS', 'CONCLUIDO_COM_ALERTAS', 'CONCLUÍDO_COM_ALERTAS'].includes(raw)) return 'COMPLETED_WITH_WARNINGS';
  if (['ERROR', 'ERRO', 'FALHOU', 'FAILED', 'CANCELLED', 'CANCELED'].includes(raw)) return 'FAILED';
  return 'QUEUED';
}

function statusMeta(status) {
  return {
    QUEUED: { label: 'Aguardando', className: 'queued' },
    PROCESSING: { label: 'Processando', className: 'processing' },
    COMPLETED: { label: 'Concluído', className: 'completed' },
    COMPLETED_WITH_WARNINGS: { label: 'Concluído com alertas', className: 'warning' },
    FAILED: { label: 'Falhou', className: 'failed' },
  }[status] || { label: 'Aguardando', className: 'queued' };
}

function mergeJob(source, payload = {}) {
  const jobPayload = payload.job && typeof payload.job === 'object' ? payload.job : payload;
  const incomingBatchId = jobPayload.batch_id ?? payload.batch_id;
  if (normalizedBatchId(incomingBatchId)) keepOnlyBatch(incomingBatchId);
  const previous = state.jobs[source] || {};
  const merged = {
    ...previous,
    ...jobPayload,
    source,
    id: jobPayload.id ?? jobPayload.job_id ?? previous.id,
    batch_id: jobPayload.batch_id ?? payload.batch_id ?? previous.batch_id,
    file: jobPayload.file ?? jobPayload.filename ?? previous.file ?? String(jobPayload.blob_path || previous.blob_path || '').split('/').pop(),
    processed_rows: asNumber(jobPayload.processed_rows ?? previous.processed_rows),
    total_rows: asNumber(jobPayload.total_rows ?? previous.total_rows),
    error_message: jobPayload.error_message ?? previous.error_message ?? '',
    warnings: jobPayload.warnings ?? previous.warnings ?? [],
    updated_at: jobPayload.updated_at || new Date().toISOString(),
  };
  merged.status = normalizeStatus(jobPayload.status ?? previous.status, merged);
  state.jobs[source] = merged;
  persistJobs();
  renderJob(source);
  renderUploadSummary();
  return merged;
}

function jobProgress(job) {
  if (!job) return 0;
  if (Number.isFinite(Number(job.progress_percent))) return Math.max(0, Math.min(100, Number(job.progress_percent)));
  if (job.total_rows > 0) return Math.max(0, Math.min(100, job.processed_rows / job.total_rows * 100));
  return SUCCESS_STATUSES.has(job.status) ? 100 : 0;
}

function renderJob(source) {
  const job = state.jobs[source];
  const badge = $(`badge-${source}`);
  const message = $(`state-${source}`);
  const progress = $(`progress-${source}`);
  const progressText = $(`progress-text-${source}`);
  const processButton = document.querySelector(`[data-process="${source}"]`);
  if (!badge || !message || !progress || !progressText || !processButton) return;

  const status = job?.status || 'QUEUED';
  const meta = statusMeta(status);
  badge.textContent = job ? meta.label : 'Aguardando arquivo';
  badge.className = `job-badge ${job ? meta.className : 'empty'}`;
  progress.value = jobProgress(job);
  progress.classList.toggle('indeterminate', status === 'PROCESSING' && !job?.total_rows);

  if (!job) {
    message.textContent = 'Selecione e envie o arquivo desta etapa.';
    progressText.textContent = '';
    processButton.disabled = true;
    processButton.textContent = 'Processar esta fonte';
    return;
  }

  const fileName = job.file || job.filename || 'arquivo enviado';
  if (status === 'FAILED') message.textContent = friendlyServerMessage(400, job.error_message || 'O processamento falhou. Corrija a fonte ou tente novamente.');
  else if (status === 'COMPLETED_WITH_WARNINGS') message.textContent = `${fileName} processado com alertas.`;
  else if (status === 'COMPLETED') message.textContent = `${fileName} processado com sucesso.`;
  else if (status === 'PROCESSING') message.textContent = `${fileName} está sendo processado em segundo plano.`;
  else message.textContent = `${fileName} enviado e pronto para processar.`;

  if (job.total_rows > 0) progressText.textContent = `${formatNumber(job.processed_rows)} / ${formatNumber(job.total_rows)} linhas (${Math.round(jobProgress(job))}%)`;
  else if (status === 'PROCESSING') progressText.textContent = `${formatNumber(job.processed_rows)} linha(s) processada(s)`;
  else if (SUCCESS_STATUSES.has(status)) progressText.textContent = `${formatNumber(job.processed_rows)} linha(s) processada(s)`;
  else progressText.textContent = `Job ${job.id}`;

  // `run_id` identifies the shared data cycle and already exists immediately
  // after upload. Only a Workflow run means the source was actually started.
  const alreadyStarted = Boolean(job.workflow_run_id) && status === 'QUEUED';
  processButton.disabled = status === 'PROCESSING' || alreadyStarted;
  processButton.textContent = status === 'FAILED'
    ? 'Tentar novamente'
    : SUCCESS_STATUSES.has(status)
      ? 'Reprocessar esta fonte'
      : alreadyStarted
        ? 'Aguardando worker'
        : 'Processar esta fonte';
}

function renderUploadSummary() {
  const jobs = UPLOAD_SOURCES.map((source) => state.jobs[source.id]).filter(Boolean);
  const completed = jobs.filter((job) => SUCCESS_STATUSES.has(job.status)).length;
  const processing = jobs.filter((job) => job.status === 'PROCESSING').length;
  const failed = jobs.filter((job) => job.status === 'FAILED').length;
  const queued = jobs.filter((job) => job.status === 'QUEUED').length;
  const parts = [`${completed}/${UPLOAD_SOURCES.length} fontes concluídas`];
  if (processing) parts.push(`${processing} processando`);
  if (queued) parts.push(`${queued} aguardando`);
  if (failed) parts.push(`${failed} com falha`);
  $('upload-status').textContent = parts.join(' · ');
  const reconcileButton = $('process-upload');
  const allComplete = UPLOAD_SOURCES.every((source) => SUCCESS_STATUSES.has(state.jobs[source.id]?.status));
  if (state.reconciliation?.status === 'RECONCILING') {
    $('upload-status').textContent = 'RECONCILING — conciliação em andamento. Esta tela atualiza automaticamente.';
    reconcileButton.disabled = true;
    reconcileButton.textContent = 'Conciliando fontes...';
    reconcileButton.title = 'A conciliação está sendo executada em segundo plano';
    return;
  }
  if (state.reconciliation?.status === 'COMPLETED') {
    $('upload-status').textContent = 'COMPLETED — conciliação concluída; o dashboard já pode ser atualizado.';
    reconcileButton.disabled = true;
    reconcileButton.textContent = 'Conciliação concluída';
    reconcileButton.title = 'Este ciclo já foi conciliado';
    return;
  }
  if (state.reconciliation?.status === 'FAILED') {
    const detail = friendlyServerMessage(400, state.reconciliation.error || 'A conciliação falhou. Tente novamente.');
    $('upload-status').textContent = `FAILED — ${detail}`;
    reconcileButton.disabled = !allComplete;
    reconcileButton.textContent = 'Tentar conciliação novamente';
    reconcileButton.title = allComplete ? 'Tentar novamente a conciliação deste ciclo' : 'Conclua todas as fontes antes de conciliar';
    return;
  }
  reconcileButton.disabled = !allComplete || state.reconciling;
  reconcileButton.textContent = 'Conciliar fontes concluídas';
  reconcileButton.title = allComplete ? 'Executar a conciliação com as fontes concluídas' : 'Conclua todas as fontes antes de conciliar';
}

function buildUploadArea() {
  const list = $('upload-list');
  list.replaceChildren();
  UPLOAD_SOURCES.forEach((source, index) => {
    const input = create('input', { id: `file-${source.id}`, type: 'file', accept: source.accept.join(','), attrs: { 'aria-label': `Arquivo para ${source.title}` } });
    const uploadButton = create('button', { type: 'button', text: 'Enviar arquivo', dataset: { upload: source.id } });
    const processButton = create('button', { type: 'button', text: 'Processar esta fonte', dataset: { process: source.id } });
    const badge = create('span', { id: `badge-${source.id}`, className: 'job-badge empty', text: 'Aguardando arquivo' });
    const stateText = create('span', { id: `state-${source.id}`, className: 'file-state', text: 'Selecione e envie o arquivo desta etapa.' });
    const progress = create('progress', { id: `progress-${source.id}`, max: 100, value: 0, attrs: { 'aria-label': `Progresso de ${source.title}` } });
    const progressText = create('small', { id: `progress-text-${source.id}`, className: 'progress-text' });
    const card = create('article', { className: 'upload-card', dataset: { source: source.id } }, [
      create('div', { className: 'upload-card-title' }, [create('strong', { text: `Etapa ${index + 1}: ${source.title}` }), badge]),
      create('small', { className: 'source-detail', text: `${source.detail} Arquivo esperado: ${source.expected}` }),
      input,
      create('div', { className: 'upload-actions' }, [uploadButton, processButton]),
      create('div', { className: 'job-state', attrs: { 'aria-live': 'polite' } }, [stateText, progress, progressText]),
    ]);
    list.append(card);
    uploadButton.addEventListener('click', () => uploadFile(source, uploadButton));
    processButton.addEventListener('click', () => processSource(source.id));
    renderJob(source.id);
  });
  renderUploadSummary();
}

function validateFile(source, file) {
  if (!file) return 'Selecione um arquivo antes de enviar.';
  const lowerName = file.name.toLowerCase();
  if (!source.accept.some((extension) => lowerName.endsWith(extension))) return `Formato inválido. Use ${source.accept.join(' ou ')}.`;
  if (file.size > MAX_UPLOAD_SIZE) return 'O arquivo excede o limite atual de 4 MB.';
  return '';
}

async function uploadFile(source, button) {
  const input = $(`file-${source.id}`);
  const file = input.files[0];
  const validation = validateFile(source, file);
  if (validation) {
    $(`state-${source.id}`).textContent = validation;
    return;
  }

  button.disabled = true;
  $(`state-${source.id}`).textContent = 'Enviando arquivo para o armazenamento seguro...';
  try {
    const form = new FormData();
    form.append('source', source.id);
    form.append('file', file);
    const data = await requestJson('/api/upload', { method: 'POST', body: form }, 180_000);
    if (!data.job_id && !data.id) throw new ApiError('O servidor recebeu o arquivo, mas não criou o job de importação.');
    mergeJob(source.id, { ...data, id: data.job_id || data.id, file: data.file || file.name, status: data.status || 'QUEUED', processed_rows: 0, total_rows: 0, error_message: '' });
  } catch (error) {
    $(`state-${source.id}`).textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function processSource(source) {
  const job = state.jobs[source];
  if (!job?.id) {
    $(`state-${source}`).textContent = 'Envie o arquivo antes de iniciar o processamento.';
    return;
  }

  mergeJob(source, { ...job, status: 'PROCESSING', error_message: '' });
  try {
    const retrying = job.status === 'FAILED';
    const restarting = SUCCESS_STATUSES.has(job.status);
    const endpoint = retrying
      ? `/api/import/${encodeURIComponent(job.id)}/retry`
      : `/api/import/${encodeURIComponent(job.id)}`;
    const data = await requestJson(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ batch_id: job.batch_id || null, restart: restarting }),
    }, 60_000);

    if (data.status || data.job) mergeJob(source, data);
    else if (data.sources) {
      const rows = asNumber(data.sources[source]);
      mergeJob(source, { ...job, processed_rows: rows, total_rows: rows, warnings: data.warnings || [], status: data.warnings?.length ? 'COMPLETED_WITH_WARNINGS' : 'COMPLETED' });
    }
    if (!TERMINAL_STATUSES.has(state.jobs[source]?.status)) schedulePoll(source, true);
  } catch (error) {
    if (error.code === 'TIMEOUT') {
      $(`state-${source}`).textContent = 'A confirmação demorou, mas o job pode continuar em segundo plano. Consultando o andamento...';
      schedulePoll(source, true);
    } else {
      mergeJob(source, { ...job, status: 'FAILED', error_message: error.message });
      $(`state-${source}`).textContent = error.message;
    }
  }
}

async function fetchJob(jobId) {
  return requestJson(`/api/import/${encodeURIComponent(jobId)}`, { method: 'GET' }, 20_000);
}

function schedulePoll(source, immediate = false) {
  window.clearTimeout(state.pollTimers.get(source));
  const timer = window.setTimeout(() => pollJob(source), immediate ? 0 : 2_500);
  state.pollTimers.set(source, timer);
}

async function pollJob(source) {
  const job = state.jobs[source];
  if (!job?.id || TERMINAL_STATUSES.has(job.status)) return;
  try {
    const data = await fetchJob(job.id);
    const updated = mergeJob(source, data);
    if (!TERMINAL_STATUSES.has(updated.status)) schedulePoll(source);
  } catch (error) {
    if (error.status === 404 || error.status === 405) {
      $(`state-${source}`).textContent = 'O job foi iniciado, mas a consulta de progresso ainda não está disponível.';
      return;
    }
    $(`state-${source}`).textContent = 'Não foi possível consultar o progresso agora. Nova tentativa em instantes.';
    schedulePoll(source, false);
  }
}

async function restoreRemoteJobs() {
  try {
    const data = await requestJson('/api/import-jobs/latest', { method: 'GET' }, 20_000);
    if (data.batch === null) {
      resetJobsForBatch();
      return;
    }
    const remoteJobs = data.jobs || {};
    const batchId = data.batch_id ?? data.batch?.id ?? (typeof data.batch === 'object' ? null : data.batch);
    keepOnlyBatch(batchId);
    if (Array.isArray(remoteJobs)) {
      for (const job of remoteJobs) {
        if (job.source && (!batchId || normalizedBatchId(job.batch_id ?? batchId) === normalizedBatchId(batchId))) {
          mergeJob(job.source, { ...job, batch_id: job.batch_id ?? batchId });
        }
      }
    } else {
      for (const [source, job] of Object.entries(remoteJobs)) {
        if (!batchId || normalizedBatchId(job.batch_id ?? batchId) === normalizedBatchId(batchId)) {
          mergeJob(source, { ...job, batch_id: job.batch_id ?? batchId });
        }
      }
    }
    const batchStatus = normalizeReconciliationStatus(data.batch?.status);
    if (batchId && batchStatus) {
      updateReconciliationState(batchId, data.batch);
      if (batchStatus === 'RECONCILING') scheduleReconciliationPoll(batchId, true);
    }
  } catch (error) {
    if (![404, 405].includes(error.status)) console.warn('Não foi possível restaurar os jobs remotos:', error.message);
  }
  for (const source of UPLOAD_SOURCES) if (state.jobs[source.id]?.status === 'PROCESSING') schedulePoll(source.id, true);
}

function normalizeReconciliationStatus(value) {
  const status = String(value || '').trim().toUpperCase().replaceAll(' ', '_');
  if (['RECONCILIATION_STARTING', 'RECONCILING', 'PROCESSING', 'RUNNING', 'IN_PROGRESS'].includes(status)) return 'RECONCILING';
  if (['COMPLETED', 'DONE', 'SUCCESS', 'SUCCEEDED'].includes(status)) return 'COMPLETED';
  if (['FAILED', 'ERROR', 'CANCELLED', 'CANCELED'].includes(status)) return 'FAILED';
  return '';
}

function updateReconciliationState(batchId, batch = {}) {
  const status = normalizeReconciliationStatus(batch.status);
  if (!status) return '';
  state.reconciliation = {
    batchId: batch.id ?? batchId,
    status,
    error: batch.error_message || batch.error || '',
  };
  state.reconciling = status === 'RECONCILING';
  renderUploadSummary();
  return status;
}

function scheduleReconciliationPoll(batchId, immediate = false) {
  window.clearTimeout(state.reconciliationPollTimer);
  state.reconciliationPollTimer = window.setTimeout(
    () => pollReconciliation(batchId),
    immediate ? 0 : 2_500,
  );
}

async function pollReconciliation(batchId) {
  try {
    const data = await requestJson(`/api/reconcile/${encodeURIComponent(batchId)}`, { method: 'GET' }, 20_000);
    // Durable reconciliation state is always read from `data.batch.status`.
    const status = updateReconciliationState(batchId, data.batch || {});
    if (status === 'RECONCILING') scheduleReconciliationPoll(batchId);
  } catch (error) {
    if (state.reconciliation?.status === 'RECONCILING') {
      $('upload-status').textContent = 'RECONCILING — não foi possível consultar o progresso agora; nova tentativa em instantes.';
      scheduleReconciliationPoll(batchId, false);
    }
  }
}

async function reconcileCompleted() {
  const button = $('process-upload');
  const jobs = UPLOAD_SOURCES.map((source) => state.jobs[source.id]);
  if (!jobs.every((job) => job?.id && SUCCESS_STATUSES.has(job.status))) {
    $('upload-status').textContent = 'Conclua todas as fontes obrigatórias antes de executar a conciliação.';
    return;
  }

  const batchIds = [...new Set(jobs.map((job) => normalizedBatchId(job.batch_id)).filter(Boolean))];
  if (batchIds.length !== 1) {
    $('upload-status').textContent = 'Os arquivos pertencem a ciclos diferentes. Atualize a página e envie novamente as fontes deste ciclo.';
    return;
  }

  state.reconciling = true;
  state.reconciliation = null;
  button.disabled = true;
  button.textContent = 'Iniciando conciliação...';
  try {
    const data = await requestJson('/api/reconcile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        job_ids: jobs.map((job) => job.id),
        batch_ids: batchIds,
      }),
    }, 60_000);
    const batchId = data.batch?.id ?? data.batch_id ?? batchIds[0];
    const status = updateReconciliationState(batchId, data.batch || { status: 'RECONCILING' });
    if (status === 'RECONCILING') scheduleReconciliationPoll(batchId, true);
    state.optionsLoaded = false;
  } catch (error) {
    state.reconciliation = { batchId: batchIds[0], status: 'FAILED', error: error.message };
  } finally {
    state.reconciling = state.reconciliation?.status === 'RECONCILING';
    renderUploadSummary();
  }
}

function statusCount(data, name) {
  return asNumber(data.statuses?.[name]);
}

function setGauge(id, value) {
  const percent = Math.max(0, Math.min(100, asNumber(value)));
  const gauge = $(id);
  gauge.style.setProperty('--eff', `${percent}%`);
  gauge.classList.toggle('empty', percent === 0);
}

function populateOptions(data) {
  const options = data.options || {};
  const oldCenter = $('center').value;
  const oldDirection = $('direction').value;
  $('center').replaceChildren(new Option('Todos os centros', ''));
  for (const center of options.centers || []) $('center').append(new Option(String(center), String(center)));
  $('direction').replaceChildren(new Option('Todas', ''));
  for (const direction of options.directions || []) $('direction').append(new Option(String(direction.label), String(direction.value)));
  if ([...$('center').options].some((option) => option.value === oldCenter)) $('center').value = oldCenter;
  if ([...$('direction').options].some((option) => option.value === oldDirection)) $('direction').value = oldDirection;
  if (!$('from').value) $('from').value = options.range?.min_date || '';
  if (!$('to').value) $('to').value = options.range?.max_date || '';
  state.optionsLoaded = true;
}

function resetDashboard() {
  for (const id of ['total', 'correct', 'pending', 'divergent', 'unposted', 'donutValue']) $(id).textContent = '0';
  $('correctPct').textContent = '0% de eficácia';
  $('efficacy').textContent = '0%';
  $('adherence').textContent = '0%';
  $('adherenceInfo').textContent = '0 lançados / 0';
  setGauge('efficacy-gauge', 0);
  setGauge('adherence-gauge', 0);
  $('donut').className = 'donut empty';
  $('donut').style.background = '';
  $('legend').replaceChildren();
  $('bars').replaceChildren(create('p', { text: 'Sem divergências no filtro.' }));
  $('issues').replaceChildren(create('p', { text: 'Nenhum alerta.' }));
  renderDashboardRows([]);
}

function renderCharts(data) {
  const values = [
    ['Corretos', statusCount(data, 'CORRETO'), '#42c96d'],
    ['Pendentes', statusCount(data, 'PENDENTE'), '#e2ab1d'],
    ['Divergentes', statusCount(data, 'DIVERGENTE'), '#e44e4a'],
    ['Não lançados', statusCount(data, 'NAO_LANCADO'), '#ac62d2'],
  ];
  const total = asNumber(data.total);
  const donut = $('donut');
  const legend = $('legend');
  legend.replaceChildren();

  if (total <= 0) {
    donut.className = 'donut empty';
    donut.style.background = '';
  } else {
    let at = 0;
    const gradient = values.map((entry) => {
      const next = at + entry[1] / total * 100;
      const stop = `${entry[2]} ${at}% ${next}%`;
      at = next;
      return stop;
    }).join(',');
    donut.className = 'donut';
    donut.style.background = `conic-gradient(${gradient})`;
  }

  for (const [name, value, color] of values) {
    const bullet = create('b', { text: '●' });
    bullet.style.color = color;
    legend.append(create('li', {}, [bullet, document.createTextNode(` ${name} (${formatNumber(value)})`)]));
  }

  const grouped = new Map();
  for (const row of data.pending || []) {
    const key = safeText(row.diagnosis || row.status);
    grouped.set(key, (grouped.get(key) || 0) + 1);
  }
  const max = Math.max(1, ...grouped.values());
  const bars = $('bars');
  bars.replaceChildren();
  if (!grouped.size) bars.append(create('p', { text: 'Sem divergências no filtro.' }));
  for (const [name, value] of grouped) {
    const bar = create('i', { className: 'bar' });
    bar.style.width = `${value / max * 100}%`;
    bars.append(create('div', {}, [create('span', { text: name }), bar, create('b', { text: formatNumber(value) })]));
  }
}

function renderDashboardRows(rows) {
  const body = $('table');
  body.replaceChildren();
  if (!rows.length) {
    const cell = create('td', { text: 'Sem pendências no filtro.', colSpan: 8 });
    body.append(create('tr', {}, cell));
    return;
  }
  for (const row of rows) {
    const values = [row.nf, row.doc_date, row.center, row.sap_material, row.manufacturer_lot, row.quantity, row.actual_quantity];
    const cells = values.map((value) => create('td', { text: safeText(value) }));
    cells.push(create('td', {}, create('span', { className: `tag ${recordStatusClass(row.status)}`.trim(), text: formatLabel(row.status) })));
    body.append(create('tr', {}, cells));
  }
}

function renderDashboard(data) {
  if (!data?.ready) {
    resetDashboard();
    $('notice').hidden = false;
    $('notice').textContent = 'Envie e processe as fontes para iniciar a auditoria.';
    return;
  }

  $('notice').hidden = true;
  if (!state.optionsLoaded && data.options) populateOptions(data);
  $('total').textContent = formatNumber(data.total);
  $('correct').textContent = formatNumber(data.correct);
  $('correctPct').textContent = `${formatNumber(data.efficacy)}% de eficácia`;
  $('efficacy').textContent = `${formatNumber(data.efficacy)}%`;
  $('adherence').textContent = `${formatNumber(data.adherence)}%`;
  $('adherenceInfo').textContent = `${formatNumber(data.launched)} lançados / ${formatNumber(data.total)}`;
  $('pending').textContent = formatNumber(statusCount(data, 'PENDENTE'));
  $('divergent').textContent = formatNumber(statusCount(data, 'DIVERGENTE'));
  $('unposted').textContent = formatNumber(statusCount(data, 'NAO_LANCADO'));
  $('donutValue').textContent = formatNumber(data.total);
  setGauge('efficacy-gauge', data.efficacy);
  setGauge('adherence-gauge', data.adherence);
  renderCharts(data);

  const issues = $('issues');
  issues.replaceChildren();
  if (!(data.issues || []).length) issues.append(create('p', { text: 'Nenhum alerta.' }));
  for (const issue of data.issues || []) {
    issues.append(create('p', {}, [
      create('strong', { text: `${safeText(issue.severity)} · ${safeText(issue.category)}` }),
      create('br'),
      document.createTextNode(safeText(issue.message)),
    ]));
  }
  renderDashboardRows(data.pending || []);
}

async function loadDashboard() {
  $('notice').hidden = false;
  $('notice').textContent = 'Atualizando indicadores...';
  try {
    const data = await requestJson(`/api/dashboard?${qs()}`, { method: 'GET' }, 45_000);
    renderDashboard(data);
  } catch (error) {
    $('notice').hidden = false;
    $('notice').textContent = error.message;
  }
}

function orderedColumns(rows, preferred = []) {
  const present = [...new Set(rows.flatMap((row) => Object.keys(row || {})))];
  const ordered = preferred.filter((column) => present.includes(column));
  return [...ordered, ...present.filter((column) => !ordered.includes(column))];
}

function isStatusColumn(column) {
  return ['status', 'situacao', 'confidence'].includes(column);
}

function recordStatusClass(value) {
  const normalized = String(value || '').toUpperCase();
  if (/CORRETO|CONCLU|ALTA|SUCESS/.test(normalized)) return 'completed';
  if (/ALERTA|PENDENTE|M[ÉE]DIA/.test(normalized)) return 'warning';
  if (/DIVERG|FALH|ERRO|N[ÃA]O_LAN[ÇC]ADO/.test(normalized)) return 'failed';
  if (/PROCESS/.test(normalized)) return 'processing';
  return '';
}

function renderPageTable(page, data) {
  const config = PAGE_CONFIG[page] || { title: formatLabel(page), description: '', empty: 'Sem registros.' };
  const rows = Array.isArray(data.rows) ? data.rows : [];
  const columns = orderedColumns(rows, config.columns || []);
  $('page-heading').textContent = config.title;
  $('page-description').textContent = config.description;
  $('page-head').replaceChildren();
  $('page-body').replaceChildren();

  if (columns.length) {
    $('page-head').append(create('tr', {}, columns.map((column) => create('th', { text: formatLabel(column) }))));
  }
  if (!rows.length) {
    $('page-body').append(create('tr', {}, create('td', { text: config.empty || 'Sem registros.', colSpan: Math.max(1, columns.length) })));
  } else {
    for (const row of rows) {
      const cells = columns.map((column) => {
        const text = safeText(row[column]);
        if (isStatusColumn(column)) return create('td', {}, create('span', { className: `tag ${recordStatusClass(text)}`.trim(), text }));
        return create('td', { text, title: text.length > 80 ? text : '' });
      });
      $('page-body').append(create('tr', {}, cells));
    }
  }
  renderPageSummary(page, data.summary);
  renderPageActions(page);
}

function renderPageSummary(page, summary) {
  const box = $('page-summary');
  box.replaceChildren();
  if (page !== 'regularization' || !summary) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.append(
    create('strong', { text: `${formatNumber(summary.total)} notas para regularizar` }),
    document.createTextNode(` · ${formatNumber(summary.entries)} entradas · ${formatNumber(summary.exits)} saídas · ${formatNumber(summary.recipes_suggested)} com receita sugerida (D ou D-1).`),
  );
}

function addExportButtons(container, page) {
  if (!['reports', 'regularization'].includes(page)) return;
  const csv = create('button', { type: 'button', text: 'Exportar CSV' });
  const excel = create('button', { type: 'button', text: 'Exportar Excel' });
  csv.addEventListener('click', () => window.location.assign(`/api/export/csv/${page}?${qs()}`));
  excel.addEventListener('click', () => window.location.assign(`/api/export/xlsx/${page}?${qs()}`));
  container.append(create('div', { className: 'export-actions' }, [csv, excel]));
}

async function addRtPreference(container) {
  try {
    const settings = await requestJson('/api/settings/rt-preference', { method: 'GET' }, 20_000);
    if (state.current !== 'regularization') return;
    const label = create('label', { className: 'rt-control' }, create('span', { text: 'Preferência de RT' }));
    const select = create('select', { id: 'rt-preference' });
    if (!(settings.options || []).length) select.append(new Option('Nenhum RT disponível', ''));
    for (const value of settings.options || []) select.append(new Option(String(value), String(value), false, value === settings.preferred_rt));
    select.addEventListener('change', async () => {
      select.disabled = true;
      try {
        await requestJson('/api/settings/rt-preference', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ preferred_rt: select.value }),
        }, 20_000);
        await loadPage('regularization');
      } catch (error) {
        $('notice').hidden = false;
        $('notice').textContent = error.message;
      } finally {
        select.disabled = false;
      }
    });
    label.append(select);
    container.prepend(label);
  } catch (error) {
    container.prepend(create('span', { className: 'inline-error', text: error.message }));
  }
}

function renderPageActions(page) {
  const actions = $('page-actions');
  actions.replaceChildren();
  addExportButtons(actions, page);
  if (page === 'regularization') addRtPreference(actions);
}

async function loadPage(page) {
  const requestId = ++state.requestSequence;
  const config = PAGE_CONFIG[page] || { title: formatLabel(page), description: '' };
  $('page-heading').textContent = config.title;
  $('page-description').textContent = config.description;
  $('page-head').replaceChildren();
  $('page-body').replaceChildren(create('tr', {}, create('td', { text: 'Carregando registros...' })));
  try {
    const data = await requestJson(`/api/page/${encodeURIComponent(page)}?${qs()}`, { method: 'GET' }, 45_000);
    if (requestId !== state.requestSequence || state.current !== page) return;
    renderPageTable(page, data);
  } catch (error) {
    if (requestId !== state.requestSequence || state.current !== page) return;
    $('page-head').replaceChildren();
    $('page-body').replaceChildren(create('tr', {}, create('td', { className: 'table-error', text: error.message })));
    $('page-actions').replaceChildren();
  }
}

function setActiveNavigation(page) {
  for (const link of document.querySelectorAll('nav a[data-page]')) link.classList.toggle('active', link.dataset.page === page);
}

function updateViewVisibility(page) {
  $('uploads-view').hidden = page !== 'uploads';
  $('dashboard-view').hidden = page !== 'dashboard';
  $('page-view').hidden = page === 'dashboard' || page === 'uploads';
  $('filters').hidden = page === 'uploads';
  if (page !== 'dashboard') $('notice').hidden = true;
}

async function navigate(page, updateHistory = true) {
  const validPage = page === 'dashboard' || page === 'uploads' || PAGE_CONFIG[page] ? page : 'dashboard';
  state.current = validPage;
  setActiveNavigation(validPage);
  updateViewVisibility(validPage);
  const config = PAGE_CONFIG[validPage];
  $('title').textContent = validPage === 'dashboard' ? 'Dashboard' : validPage === 'uploads' ? 'Importar arquivos' : config.title;
  $('subtitle').textContent = validPage === 'dashboard'
    ? 'Visão geral da auditoria e conciliação de movimentações de químicos'
    : validPage === 'uploads'
      ? 'Fluxo controlado de envio, processamento e conciliação por fonte'
      : config.description;
  if (updateHistory && window.location.hash !== `#${validPage}`) history.pushState({ page: validPage }, '', `#${validPage}`);
  if (validPage === 'dashboard') await loadDashboard();
  else if (validPage === 'uploads') {
    renderUploadSummary();
    for (const source of UPLOAD_SOURCES) renderJob(source.id);
  } else await loadPage(validPage);
}

async function refreshCurrentView() {
  const button = $('refresh-data');
  button.disabled = true;
  button.textContent = 'Atualizando...';
  state.optionsLoaded = false;
  try {
    if (state.current === 'uploads') {
      await restoreRemoteJobs();
      renderUploadSummary();
    } else if (state.current === 'dashboard') await loadDashboard();
    else await loadPage(state.current);
  } finally {
    button.disabled = false;
    button.textContent = '↻ Atualizar dados';
  }
}

function installEvents() {
  for (const link of document.querySelectorAll('nav a[data-page]')) {
    link.addEventListener('click', (event) => {
      event.preventDefault();
      navigate(link.dataset.page);
    });
  }
  $('apply').addEventListener('click', () => state.current === 'dashboard' ? loadDashboard() : loadPage(state.current));
  $('refresh-data').addEventListener('click', refreshCurrentView);
  $('process-upload').addEventListener('click', reconcileCompleted);
  window.addEventListener('popstate', () => navigate(window.location.hash.slice(1) || 'dashboard', false));
}

async function init() {
  buildUploadArea();
  installEvents();
  await restoreRemoteJobs();
  await navigate(window.location.hash.slice(1) || 'dashboard', false);
}

init();
