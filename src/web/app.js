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
  pending: { title: 'Pendências', description: 'Análise lote a lote dos documentos que exigem conferência.', empty: 'Nenhuma pendência encontrada.', columns: ['status', 'situacao_descricao', 'acao_recomendada', 'diagnosis', 'confidence', 'classificacao_automacao', 'nf', 'series', 'doc_date', 'center', 'direcao', 'sap_material', 'lote_sap', 'lote_fabricante', 'quantidade_sap', 'unidade_sap', 'produto_sisdev', 'lote_sisdev', 'quantidade_sisdev'] },
  work_queue: { title: 'Fila de trabalho', description: 'Pendências do dia com responsável, prioridade, prazo e histórico de tratamento.', empty: 'Nenhuma tarefa pendente.', columns: ['prioridade_trabalho', 'andamento', 'situacao_prazo', 'prazo', 'responsavel', 'numero_nfe', 'serie', 'data_documento', 'centro', 'direcao', 'situacao', 'acao_recomendada', 'comentarios', '__work_actions'] },
  regularization: { title: 'Regularizar SISDEV', description: 'Fila inteligente por nota fiscal: resumo primeiro e rastreabilidade sob demanda.', empty: 'Nenhuma nota fiscal para regularizar.', columns: ['prioridade', 'situacao', 'diagnostico_situacao', 'status_saldo', 'acao_recomendada', 'numero_nfe', 'serie', 'data_documento', 'cnpj', 'produto', 'volume_embalagem', 'quantidade_embalagem', 'itens_resumo', '__detail'] },
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
  logs: { title: 'Logs', description: 'Trilha imutável das ações críticas realizadas na plataforma.', empty: 'Nenhuma ação registrada.', columns: ['id', 'data_hora', 'usuario', 'acao', 'modulo', 'entidade', 'identificador', 'resultado', 'justificativa'] },
  users: { title: 'Usuários', description: 'Usuários, perfis, permissões e escopos autorizados.', empty: 'Nenhum usuário cadastrado.', columns: ['display_name', 'email', 'profile', 'active', 'scopes', '__user_actions'] },
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
  nome_rt: 'Agrônomo/Técnico', dose_recomendada: 'Dose recomendada', ure: 'URE', art: 'ART/TRT', cnpj: 'CNPJ',
  situacao_descricao: 'Diagnóstico da situação', status_saldo: 'Status saldo', acao_recomendada: 'Ação recomendada',
  classificacao_automacao: 'Classificação de confiança', data_hora: 'Data/hora', usuario: 'Usuário',
  modulo: 'Módulo', entidade: 'Entidade', identificador: 'Identificador', resultado: 'Resultado',
  display_name: 'Nome', email: 'Usuário/e-mail', profile: 'Perfil', active: 'Ativo', scopes: 'Escopos',
  diagnostico: 'Alvo/Problema', area_receita: 'Área tratada (receita)', numero_nfe: 'Nº da NF',
  serie: 'Série da NF', data_documento: 'Data da NF', volume_embalagem: 'Volume embalagem',
  quantidade_embalagem: 'Qnt. embalagem', fornecedor: 'Fornecedor', status_conciliacao: 'Status da conciliação',
  prioridade: 'Prioridade', diagnostico_situacao: 'Diagnóstico da situação', itens_resumo: 'Itens/Lotes',
  situacao: 'Situação',
  status_lancamento_sap: 'Status de lançamento SAP', status_operacional: 'Status operacional',
  compatibilidade: 'Compatibilidade', tratamento_status: 'Tratamento',
  emitente: 'Emitente', tipo_entrada: 'Tipo de entrada',
  prioridade_trabalho: 'Prioridade', andamento: 'Andamento', responsavel: 'Responsável',
  prazo: 'Prazo', comentarios: 'Comentários',
  situacao_prazo: 'Situação do prazo',
};

const state = {
  current: 'dashboard',
  optionsLoaded: false,
  jobs: loadStoredJobs(),
  pollTimers: new Map(),
  requestSequence: 0,
  detailSequence: 0,
  reconciling: false,
  reconciliation: null,
  reconciliationPollTimer: 0,
  importHealthTimer: 0,
  auth: null,
  page: 1,
  perPage: 50,
  sort: '',
  order: 'asc',
  appInitialized: false,
  editingUserId: null,
  regularizationLotOptions: new Map(),
  lotTraceOptions: new Map(),
  lotTraceSelection: null,
  lotTraceSearchTimer: 0,
  workQueueAssignees: [],
  editingWorkItem: null,
  workQueuePriority: '',
  workQueueAssignee: '',
  workQueueDueStatus: '',
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

function displayValue(column, value) {
  if (value === null || value === undefined || value === '') return '—';
  const name = String(column || '').toLowerCase();
  const raw = String(value);
  if ((name.includes('date') || name.includes('data') || name.endsWith('_at')) && /^\d{4}-\d{2}-\d{2}/.test(raw)) {
    const [datePart, timePart] = raw.split(/[T ]/);
    const [year, month, day] = datePart.split('-');
    return timePart ? `${day}/${month}/${year} ${timePart.slice(0, 8)}` : `${day}/${month}/${year}`;
  }
  if (typeof value === 'boolean') return value ? 'Sim' : 'Não';
  return safeText(value);
}

function qs() {
  const lotInput = state.current === 'regularization' ? $('lot-filter') : null;
  const lotSearch = lotInput?.value.trim() || '';
  const selectedLot = state.regularizationLotOptions.get(lotSearch.toLocaleUpperCase('pt-BR')) || null;
  return new URLSearchParams({
    from: $('from').value,
    to: $('to').value,
    center: $('center').value,
    direction: $('direction').value,
    preferred_rt: $('rt-preference')?.value || '',
    status: $('status-filter')?.value || '',
    nf: $('nf-filter')?.value.trim() || '',
    product: selectedLot?.product || '',
    lot: selectedLot?.lot || '',
    lot_search: selectedLot ? '' : lotSearch,
    page: String(state.page),
    per_page: String(state.perPage),
    sort: state.sort,
    order: state.order,
    priority: state.current === 'work_queue' ? state.workQueuePriority : '',
    assigned_to: state.current === 'work_queue' ? state.workQueueAssignee : '',
    due_status: state.current === 'work_queue' ? state.workQueueDueStatus : '',
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
    const headers = new Headers(options.headers || {});
    const method = String(options.method || 'GET').toUpperCase();
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && state.auth?.csrfToken && url !== '/api/auth/login') {
      headers.set('X-CSRF-Token', state.auth.csrfToken);
    }
    response = await fetch(url, { ...options, headers, signal: controller.signal });
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
  if (!response.ok) {
    if (response.status === 401 && url !== '/api/auth/login') showLogin('Sua sessão expirou. Entre novamente.');
    throw new ApiError(friendlyServerMessage(response.status, data.error || data.message, raw), response.status, data.code || 'HTTP_ERROR');
  }
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
    MISSING: { label: 'Arquivo pendente', className: 'empty' },
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

function formatDuration(totalSeconds) {
  const seconds = Math.max(0, asNumber(totalSeconds));
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `${hours} h ${minutes % 60} min`;
}

function importSourceTitle(sourceId) {
  return UPLOAD_SOURCES.find((source) => source.id === sourceId)?.title || formatLabel(sourceId);
}

function importHealthMetric(label, value, className = '') {
  return create('article', { className }, [
    create('small', { text: label }),
    create('strong', { text: String(value) }),
  ]);
}

function renderImportHealth(data) {
  const summary = data.summary || {};
  $('import-health-metrics').replaceChildren(
    importHealthMetric('Ciclo concluído', `${formatNumber(summary.completed)}/${formatNumber(summary.required)}`, 'health-ok'),
    importHealthMetric('Processando', formatNumber(summary.processing), 'health-running'),
    importHealthMetric('Aguardando', formatNumber(asNumber(summary.waiting) + asNumber(summary.missing)), 'health-waiting'),
    importHealthMetric('Com falha', formatNumber(summary.failed), 'health-failed'),
    importHealthMetric('Prontidão', `${formatNumber(summary.progress_percent)}%`),
  );

  const alerts = Array.isArray(data.alerts) ? data.alerts : [];
  $('import-health-alert-list').replaceChildren(...alerts.map((alert) => create('p', { text: alert })));
  $('import-health-batch').textContent = data.batch?.id
    ? `Ciclo ${data.batch.id} · ${displayValue('created_at', data.batch.created_at)}`
    : 'Nenhum ciclo iniciado';

  const body = $('import-health-body');
  body.replaceChildren();
  const sources = Array.isArray(data.sources) ? data.sources : [];
  if (!sources.length) {
    body.append(create('tr', {}, create('td', { colSpan: 9, text: 'Envie a primeira fonte para iniciar o acompanhamento.' })));
  } else {
    for (const source of sources) {
      const status = String(source.status || 'MISSING');
      const meta = statusMeta(status);
      const progress = Math.max(0, Math.min(100, asNumber(source.progress_percent)));
      const progressCell = create('td', { className: 'health-progress' }, [
        create('progress', { max: 100, value: progress, attrs: { 'aria-label': `Progresso de ${importSourceTitle(source.source)}` } }),
        create('small', { text: source.total_rows > 0
          ? `${formatNumber(source.processed_rows)} / ${formatNumber(source.total_rows)} (${formatNumber(progress)}%)`
          : `${formatNumber(source.processed_rows)} linha(s)` }),
      ]);
      const activity = source.updated_at
        ? `${displayValue('updated_at', source.updated_at)}${source.stale ? ' · desatualizada' : ''}`
        : '—';
      body.append(create('tr', {}, [
        create('td', {}, [create('strong', { text: importSourceTitle(source.source) }), create('small', { className: 'source-detail', text: source.source_file || 'Arquivo ainda não enviado' })]),
        create('td', {}, create('span', { className: `job-badge ${meta.className}`, text: source.status_label || meta.label })),
        progressCell,
        create('td', { text: formatNumber(source.inserted_rows) }),
        create('td', { text: formatNumber(source.duplicate_rows) }),
        create('td', { text: formatNumber(source.error_rows) }),
        create('td', { text: formatDuration(source.duration_seconds) }),
        create('td', { className: source.stale ? 'health-stale' : '', text: activity, title: source.last_message || '' }),
        create('td', { text: source.action_recommended || 'Verificar a fonte.', title: source.error_message || source.last_message || '' }),
      ]));
    }
  }

  window.clearTimeout(state.importHealthTimer);
  const active = sources.some((source) => ['STARTING', 'PROCESSING'].includes(source.status)
    || (source.status === 'QUEUED' && source.workflow_run_id));
  if (state.current === 'import_health' && active) {
    state.importHealthTimer = window.setTimeout(loadImportHealth, 5_000);
  }
}

async function loadImportHealth() {
  window.clearTimeout(state.importHealthTimer);
  try {
    const data = await requestJson('/api/import-health', { method: 'GET' }, 20_000);
    if (state.current === 'import_health') renderImportHealth(data);
  } catch (error) {
    if (state.current !== 'import_health') return;
    $('import-health-alert-list').replaceChildren(create('p', { className: 'inline-error', text: error.message }));
  }
}

function populateLotTraceOptions(options = []) {
  const list = $('lot-trace-options');
  state.lotTraceOptions = new Map();
  list.replaceChildren();
  for (const option of options) {
    const label = String(option.label || '');
    state.lotTraceOptions.set(label.toLocaleUpperCase('pt-BR'), {
      product: String(option.product || ''), lot: String(option.lot || ''),
    });
    list.append(new Option(label, label));
  }
}

async function loadLotTraceOptions(search = '') {
  try {
    const data = await requestJson(`/api/lot-trace/options?search=${encodeURIComponent(search)}`, { method: 'GET' }, 20_000);
    if (state.current === 'lot_trace') populateLotTraceOptions(data.options || []);
  } catch (error) {
    if (state.current === 'lot_trace') $('lot-trace-notice').textContent = error.message;
  }
}

function selectedLotTrace() {
  const value = $('lot-trace-search').value.trim();
  const exact = state.lotTraceOptions.get(value.toLocaleUpperCase('pt-BR'));
  if (exact) return exact;
  const separator = value.lastIndexOf('&');
  if (separator >= 0) {
    return { product: value.slice(0, separator).trim(), lot: value.slice(separator + 1).trim() };
  }
  return { product: '', lot: value };
}

function renderLotTracePagination(pagination) {
  const container = $('lot-trace-pagination');
  container.replaceChildren();
  if (!pagination || pagination.total <= pagination.per_page) {
    container.hidden = true;
    return;
  }
  container.hidden = false;
  const label = create('span', { text: `Mostrando ${formatNumber(pagination.from)}–${formatNumber(pagination.to)} de ${formatNumber(pagination.total)} eventos` });
  const size = create('select', { attrs: { 'aria-label': 'Eventos por página' } });
  for (const value of [25, 50, 100, 250]) size.append(new Option(`${value} por página`, String(value), false, Number(pagination.per_page) === value));
  size.addEventListener('change', () => { state.perPage = Number(size.value); state.page = 1; searchLotTrace(false); });
  const previous = create('button', { type: 'button', className: 'secondary', text: 'Anterior', disabled: pagination.page <= 1 });
  const next = create('button', { type: 'button', className: 'secondary', text: 'Próxima', disabled: pagination.page >= pagination.total_pages });
  previous.addEventListener('click', () => { state.page = Math.max(1, pagination.page - 1); searchLotTrace(false); });
  next.addEventListener('click', () => { state.page = pagination.page + 1; searchLotTrace(false); });
  container.append(label, create('div', { className: 'pagination-controls' }, [
    size, previous, create('span', { text: `Página ${pagination.page} de ${pagination.total_pages}` }), next,
  ]));
}

function renderLotTrace(data) {
  const summary = data.summary || {};
  $('lot-trace-notice').textContent = data.balance_note || '';
  $('lot-trace-metrics').hidden = false;
  $('lot-trace-metrics').replaceChildren(
    importHealthMetric('Eventos', formatNumber(summary.events)),
    importHealthMetric('Movimentos SAP', formatNumber(summary.sap_movements), 'health-running'),
    importHealthMetric('Movimentos SISDEV', formatNumber(summary.sisdev_movements), 'health-ok'),
    importHealthMetric('Receitas', formatNumber(summary.recipes)),
    importHealthMetric('Tratamentos', formatNumber(summary.audit_actions), 'health-waiting'),
  );

  const stock = Array.isArray(data.stock) ? data.stock : [];
  $('lot-trace-stock-panel').hidden = false;
  $('lot-trace-stock').replaceChildren(...(stock.length ? stock.map((row) =>
    create('article', {}, [
      create('strong', { text: `${row.centro || 'Centro não identificado'} · ${row.material || data.product}` }),
      create('span', { text: `SAP: ${formatNumber(row.quantidade_sap)} ${row.unidade || ''}` }),
      create('span', { text: `SISDEV: ${formatNumber(row.quantidade_sisdev)} ${row.unidade || ''}` }),
      create('span', { text: `Diferença: ${formatNumber(row.diferenca_sisdev_menos_sap)} ${row.unidade || ''}` }),
    ])
  ) : [create('p', { text: 'Nenhum saldo oficial correspondente foi localizado nos arquivos de estoque.' })]));

  $('lot-trace-table-panel').hidden = false;
  $('lot-trace-title').textContent = `${data.product || 'Produto'} & ${data.lot}`;
  $('lot-trace-note').textContent = `Execução ${data.run?.id || '—'}`;
  const body = $('lot-trace-body');
  body.replaceChildren();
  const events = Array.isArray(data.events) ? data.events : [];
  if (!events.length) {
    body.append(create('tr', {}, create('td', { colSpan: 10, text: 'Nenhum movimento ou documento relacionado foi localizado.' })));
  } else {
    for (const event of events) {
      const signed = asNumber(event.signed_quantity);
      const quantityText = event.quantity === null || event.quantity === undefined
        ? '—'
        : `${signed > 0 ? '+' : signed < 0 ? '−' : ''}${formatNumber(Math.abs(signed || asNumber(event.quantity)))} ${event.unit || ''}`;
      body.append(create('tr', {}, [
        create('td', { text: displayValue('date', event.date) }),
        create('td', {}, create('span', { className: `trace-origin ${String(event.origin || '').toLowerCase()}`, text: event.origin })),
        create('td', { text: event.event_type || 'Movimento' }),
        create('td', { text: `${event.document || '—'}${event.series ? ` · Série ${event.series}` : ''}` }),
        create('td', { text: event.center || '—' }),
        create('td', { className: signed > 0 ? 'signed-positive' : signed < 0 ? 'signed-negative' : '', text: quantityText }),
        create('td', { text: `${formatNumber(event.sap_running_balance)} ${event.balance_unit || ''}` }),
        create('td', { text: `${formatNumber(event.sisdev_running_balance)} ${event.balance_unit || ''}` }),
        create('td', {}, create('span', { className: `tag ${recordStatusClass(event.status)}`.trim(), text: event.status || '—' })),
        create('td', { text: event.details || '—', title: event.details || '' }),
      ]));
    }
  }
  renderLotTracePagination(data.pagination);
}

async function searchLotTrace(resetPage = true) {
  const selection = selectedLotTrace();
  if (!selection.lot) {
    $('lot-trace-notice').textContent = 'Digite ou selecione um lote para pesquisar.';
    return;
  }
  if (resetPage) state.page = 1;
  state.lotTraceSelection = selection;
  $('lot-trace-notice').textContent = 'Montando a linha do tempo do lote...';
  try {
    const params = new URLSearchParams({
      product: selection.product, lot: selection.lot,
      page: String(state.page), per_page: String(state.perPage),
    });
    const data = await requestJson(`/api/lot-trace?${params}`, { method: 'GET' }, 45_000);
    if (state.current === 'lot_trace') renderLotTrace(data);
  } catch (error) {
    if (state.current === 'lot_trace') $('lot-trace-notice').textContent = error.message;
  }
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

function populateRegularizationLotOptions(options = []) {
  const input = $('lot-filter');
  const list = $('lot-filter-options');
  const previousValue = input.value;
  state.regularizationLotOptions = new Map();
  list.replaceChildren();
  for (const item of options) {
    const label = String(item.label || '');
    state.regularizationLotOptions.set(label.toLocaleUpperCase('pt-BR'), {
      product: String(item.product || ''),
      lot: String(item.lot || ''),
    });
    list.append(new Option(label, label));
  }
  input.value = previousValue;
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
  const internal = new Set(['reconciliation_id', 'status_original']);
  const present = [...new Set(rows.flatMap((row) => Object.keys(row || {})))].filter((column) => !internal.has(column));
  const ordered = preferred.filter((column) => present.includes(column));
  return [...ordered, ...present.filter((column) => !ordered.includes(column))];
}

function isStatusColumn(column) {
  return ['status', 'situacao', 'confidence', 'andamento'].includes(column);
}

function recordStatusClass(value) {
  const normalized = String(value || '').toUpperCase();
  if (/CORRETO|CONCLU|REGULARIZADA|VALIDADA|NO_PRAZO|ALTA|SUCESS/.test(normalized)) return 'completed';
  if (/NOVA|VENCE_HOJE|SEM_PRAZO|AGUARDANDO|ALERTA|PENDENTE|M[ÉE]DIA/.test(normalized)) return 'warning';
  if (/ATRASADA|DIVERG|FALH|ERRO|N[ÃA]O_LAN[ÇC]ADO/.test(normalized)) return 'failed';
  if (/PROCESS|AN[ÁA]LISE/.test(normalized)) return 'processing';
  return '';
}

function renderPageTable(page, data) {
  const config = PAGE_CONFIG[page] || { title: formatLabel(page), description: '', empty: 'Sem registros.' };
  const rows = Array.isArray(data.rows) ? data.rows : [];
  const preferredColumns = config.columns || [];
  const columns = page === 'regularization'
    ? preferredColumns.filter((column) => column === '__detail' || rows.some((row) => Object.hasOwn(row, column)))
    : page === 'users'
      ? preferredColumns.filter((column) => column === '__user_actions' || rows.some((row) => Object.hasOwn(row, column)))
      : page === 'work_queue'
        ? preferredColumns.filter((column) => column === '__work_actions' || rows.some((row) => Object.hasOwn(row, column)))
      : orderedColumns(rows, preferredColumns);
  if (page === 'regularization') populateRegularizationLotOptions(data.filter_options?.product_lots || []);
  if (page === 'pending' && rows.some((row) => row.reconciliation_id)) columns.push('__actions');
  $('page-heading').textContent = config.title;
  $('page-description').textContent = config.description;
  $('page-view').classList.toggle('regularization-page', page === 'regularization');
  $('page-head').replaceChildren();
  $('page-body').replaceChildren();

  if (columns.length) {
    $('page-head').append(create('tr', {}, columns.map((column) => {
       const special = column === '__actions' || column === '__detail' || column === '__user_actions' || column === '__work_actions';
       const button = create('button', { type: 'button', className: 'sort-button', text: column === '__actions' ? 'Tratamento' : column === '__detail' ? 'Detalhe' : column === '__user_actions' ? 'Ações' : column === '__work_actions' ? 'Tratar' : formatLabel(column) });
       if (special) return create('th', {}, button);
      if (state.sort === column) button.textContent += state.order === 'asc' ? ' ↑' : ' ↓';
      button.addEventListener('click', () => {
        state.order = state.sort === column && state.order === 'asc' ? 'desc' : 'asc';
        state.sort = column;
        state.page = 1;
        loadPage(page);
      });
      return create('th', {}, button);
    })));
  }
  if (!rows.length) {
    $('page-body').append(create('tr', {}, create('td', { text: config.empty || 'Sem registros.', colSpan: Math.max(1, columns.length) })));
  } else {
    for (const row of rows) {
      const cells = columns.map((column) => {
        if (column === '__detail') {
          const button = create('button', { type: 'button', className: 'detail-button', text: 'Ver detalhe' });
          button.addEventListener('click', () => openRegularizationDetail(row));
          return create('td', { className: 'detail-action-cell' }, button);
        }
        if (column === '__user_actions') {
          const edit = create('button', { type: 'button', className: 'secondary compact-action', text: 'Editar' });
          const remove = create('button', { type: 'button', className: 'danger compact-action', text: 'Excluir' });
          edit.addEventListener('click', () => openUserDialog(row));
          remove.addEventListener('click', () => deleteExistingUser(row, remove));
          return create('td', { className: 'user-action-cell' }, [edit, remove]);
        }
        if (column === '__work_actions') {
          const button = create('button', { type: 'button', className: 'detail-button', text: 'Abrir tarefa' });
          button.addEventListener('click', () => openWorkItem(row));
          return create('td', { className: 'detail-action-cell' }, button);
        }
        if (column === '__actions') {
          const button = create('button', { type: 'button', className: 'secondary', text: 'Registrar' });
          button.addEventListener('click', async () => {
            const reason = window.prompt('Descreva o tratamento ou a justificativa:');
            if (!reason?.trim()) return;
            button.disabled = true;
            try {
              await requestJson(`/api/pending/${row.reconciliation_id}/actions`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'REGISTER_NOTE', reason: reason.trim() }),
              }, 20_000);
              $('notice').hidden = false;
              $('notice').textContent = 'Tratamento registrado na trilha de auditoria.';
            } catch (error) {
              $('notice').hidden = false;
              $('notice').textContent = error.message;
            } finally {
              button.disabled = false;
            }
          });
          return create('td', {}, button);
        }
        const text = displayValue(column, row[column]);
        if (column === 'prioridade' || column === 'prioridade_trabalho') {
          return create('td', {}, create('span', { className: `priority-badge priority-${String(row[column] || '').toLowerCase()}`, text }));
        }
        if (column === 'diagnostico_situacao' || column === 'acao_recomendada' || column === 'status_saldo') {
          return create('td', { className: `regularization-explanation regularization-${column}`, title: text },
            create('div', { className: 'cell-clamp', text }));
        }
        if (isStatusColumn(column) || column === 'situacao_prazo') return create('td', {}, create('span', { className: `tag ${recordStatusClass(text)}`.trim(), text }));
        return create('td', { text, title: text.length > 80 ? text : '' });
      });
      const rowClass = page === 'regularization' ? `regularization-row priority-${String(row.prioridade || '').toLowerCase()}` : '';
      $('page-body').append(create('tr', { className: rowClass }, cells));
    }
  }
  renderPageSummary(page, data.summary);
  renderPageActions(page);
  renderPagination(page, data.pagination);
}

function detailGrid(fields) {
  return create('div', { className: 'detail-grid' }, fields.map(([label, value]) =>
    create('div', { className: 'detail-field' }, [
      create('span', { text: label }), create('strong', { text: safeText(value) }),
    ]),
  ));
}

function detailTable(rows, columns) {
  const table = create('table', { className: 'detail-table' });
  table.append(
    create('thead', {}, create('tr', {}, columns.map(([key, label]) => create('th', { text: label || formatLabel(key) })))),
    create('tbody', {}, (rows || []).length
      ? rows.map((row) => create('tr', {}, columns.map(([key]) => create('td', { text: displayValue(key, row?.[key]) }))))
      : create('tr', {}, create('td', { text: 'Nenhum dado relacionado.', colSpan: columns.length }))),
  );
  return create('div', { className: 'table-scroll' }, table);
}

function traceSection(title, content, open = false, badge = '') {
  const section = create('details', { className: 'trace-section', open });
  const summary = create('summary', {}, [create('strong', { text: title })]);
  if (badge) summary.append(create('span', { className: 'trace-count', text: badge }));
  section.append(summary, content);
  return section;
}

function currentRecipeSelection(detail) {
  const decision = (detail.decisions || []).find((item) => ['CONFIRM_RECIPE', 'SELECT_RECIPE'].includes(item.decision_type));
  return new Set((decision?.selected_ids || []).map(String));
}

function recipeSelectionPanel(detail) {
  const recipes = detail.recipes || [];
  if (!recipes.length) return create('p', { className: 'detail-empty', text: 'Nenhuma receita compatível foi localizada em D ou D-1.' });
  const selected = currentRecipeSelection(detail);
  const mode = detail.recipe_selection?.mode === 'MULTIPLE' ? 'checkbox' : 'radio';
  const required = asNumber(detail.recipe_selection?.required);
  const total = create('strong', { text: '0' });
  const difference = create('strong', { text: formatNumber(required) });
  const status = create('span', { className: 'tag warning', text: 'INCOMPATÍVEL' });
  const cards = create('div', { className: 'recipe-options' });
  const inputs = [];

  const refreshTotals = () => {
    const chosen = inputs.filter((input) => input.checked);
    const amount = chosen.reduce((sum, input) => sum + asNumber(input.dataset.quantity), 0);
    const delta = required - amount;
    total.textContent = formatNumber(amount);
    difference.textContent = formatNumber(delta);
    const compatible = Math.abs(delta) <= Math.max(.001, required * .000001);
    status.textContent = compatible ? 'COMPATÍVEL' : delta > 0 ? 'FALTA VOLUME' : 'EXCEDE VOLUME';
    status.className = `tag ${compatible ? 'completed' : 'warning'}`;
  };

  for (const recipe of recipes) {
    const input = create('input', {
      type: mode, name: `recipe-${detail.document_id}`, value: String(recipe.recipe_id),
      checked: selected.has(String(recipe.recipe_id)),
      dataset: { quantity: String(recipe.quantidade_receita || 0) },
      attrs: { 'aria-label': `Selecionar receita ${safeText(recipe.numero_receita)}` },
    });
    input.addEventListener('change', refreshTotals);
    inputs.push(input);
    const heading = create('div', { className: 'recipe-option-heading' }, [
      input,
      create('strong', { text: `Receita ${safeText(recipe.numero_receita)}` }),
      create('span', { className: 'compatibility', text: `${formatNumber(recipe.compatibilidade)}%` }),
    ]);
    if (recipe.mais_compativel) heading.append(create('span', { className: 'best-match', text: 'Mais compatível' }));
    cards.append(create('article', { className: 'recipe-option' }, [
      heading,
      detailGrid([
        ['Produto', recipe.produto], ['Material', recipe.material], ['Lote', recipe.lote],
        ['Volume relacionado', `${formatNumber(recipe.quantidade_receita)} ${safeText(recipe.unidade_receita)}`],
        ['Cultura', recipe.cultura], ['Alvo/Problema', recipe.diagnostico],
        ['Área tratada', recipe.area_receita], ['Agrônomo/Técnico', recipe.nome_rt],
        ['ART/TRT', recipe.art], ['URE', recipe.ure], ['Data', displayValue('data_emissao', recipe.data_emissao)],
        ['Motivos', (recipe.compatibilidade_motivos || []).join(', ')],
      ]),
    ]));
  }
  const justification = create('textarea', { className: 'decision-justification', attrs: { placeholder: 'Justificativa ou observação da decisão (opcional)', rows: '3' } });
  const message = create('span', { className: 'decision-message', attrs: { role: 'status' } });
  const selectedIds = () => inputs.filter((input) => input.checked).map((input) => input.value);
  const save = create('button', { type: 'button', className: 'secondary', text: 'Salvar seleção' });
  const confirm = create('button', { type: 'button', text: 'Confirmar receita' });
  const reject = create('button', { type: 'button', className: 'danger-secondary', text: 'Rejeitar opções' });
  const act = async (type, button) => {
    button.disabled = true;
    try {
      await saveRegularizationDecision(detail.document_id, type, selectedIds(), '', justification.value);
      message.textContent = type === 'CONFIRM_RECIPE' ? 'Receita confirmada e auditada.' : type === 'REJECT_RECIPE' ? 'Rejeição registrada.' : 'Seleção salva.';
    } catch (error) { message.textContent = error.message; }
    finally { button.disabled = false; }
  };
  save.addEventListener('click', () => act('SELECT_RECIPE', save));
  confirm.addEventListener('click', () => act('CONFIRM_RECIPE', confirm));
  reject.addEventListener('click', () => act('REJECT_RECIPE', reject));
  refreshTotals();
  return create('div', { className: 'recipe-selection' }, [
    create('p', { className: 'selection-mode', text: mode === 'checkbox' ? 'Seleção múltipla permitida' : 'Seleção única' }),
    cards,
    create('div', { className: 'selection-totals' }, [
      create('span', {}, ['Necessário: ', create('strong', { text: formatNumber(required) })]),
      create('span', {}, ['Selecionado: ', total]), create('span', {}, ['Diferença: ', difference]), status,
    ]),
    justification,
    create('div', { className: 'decision-actions' }, [save, reject, confirm]), message,
  ]);
}

async function saveRegularizationDecision(documentId, decisionType, selectedIds = [], movementId = '', justification = '') {
  return requestJson(`/api/regularization/${encodeURIComponent(documentId)}/decisions`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision_type: decisionType, selected_ids: selectedIds, movement_id: movementId, justification }),
  }, 30_000);
}

function returnChainPanel(detail) {
  const chain = detail.return_chain;
  if (!chain) return create('div', { className: 'return-chain' }, [
    create('p', { className: 'detail-empty', text: 'Entrada de fornecedor: não exige receita nem estorno de saída.' }),
    detailGrid([
      ['Emitente', detail.document?.emitente], ['NF de entrada', detail.document?.numero_nfe],
      ['Tipo de entrada', detail.document?.tipo_entrada], ['Itens/Lotes', detail.document?.itens_resumo],
    ]),
  ]);
  const originals = chain.originals || (chain.original ? [chain.original] : []);
  const movements = chain.movements || (chain.movement ? [chain.movement] : []);
  const selectedDecision = (detail.decisions || []).find((item) => item.decision_type === 'CONFIRM_MOVEMENT');
  const selected = new Set((selectedDecision?.selected_ids || []).map(String));
  const content = create('div', { className: 'return-chain' }, [
    detailGrid([
      ['NF de retorno', detail.document?.numero_nfe], ['Emitente', detail.document?.emitente],
      ['Quantidade retornada', chain.required_quantity], ['Quantidade localizada nas saídas', chain.matched_quantity],
      ['Diferença ainda não localizada', chain.remaining_quantity], ['Saídas do lote', originals.length],
      ['Movimentações SISDEV localizadas', movements.length], ['Status', chain.status],
    ]),
  ]);
  if (originals.length) {
    content.append(create('h3', { text: 'Notas de saída relacionadas ao lote' }));
    content.append(detailTable(originals, [
      ['nf', 'NF de saída'], ['series', 'Série'], ['doc_date', 'Data'], ['cnpj', 'CNPJ'],
      ['center', 'Centro'], ['sap_material', 'Material'], ['manufacturer_lot', 'Lote fabricante'],
      ['quantity', 'Quantidade'], ['unit', 'Unidade'], ['movement_id', 'Movimentação SISDEV'],
      ['movement_status', 'Situação SISDEV'],
    ]));
  }
  if (movements.length) {
    const inputs = [];
    const choices = create('div', { className: 'movement-options' });
    for (const movement of movements) {
      const input = create('input', {
        type: 'checkbox', value: String(movement.id), checked: selected.has(String(movement.id)),
        attrs: { 'aria-label': `Selecionar movimentação ${movement.id}` },
      });
      inputs.push(input);
      choices.append(create('label', { className: 'movement-option' }, [
        input,
        create('span', { text: `Movimentação #${movement.id} · NF ${safeText(movement.nf)} · ${safeText(movement.product)} · lote ${safeText(movement.lot)} · ${formatNumber(movement.quantidade_total)} ${safeText(movement.unit)}` }),
      ]));
    }
    const note = create('textarea', { className: 'decision-justification', attrs: { placeholder: 'Justificativa da confirmação/rejeição', rows: '3' } });
    const message = create('span', { className: 'decision-message', attrs: { role: 'status' } });
    const confirm = create('button', { type: 'button', text: 'Confirmar movimentações selecionadas' });
    const reject = create('button', { type: 'button', className: 'danger-secondary', text: 'Rejeitar seleção' });
    const act = async (type, button) => {
      const selectedIds = inputs.filter((input) => input.checked).map((input) => input.value);
      if (!selectedIds.length) {
        message.textContent = 'Selecione ao menos uma movimentação.';
        return;
      }
      button.disabled = true;
      try {
        await saveRegularizationDecision(detail.document_id, type, selectedIds, '', note.value);
        message.textContent = type === 'CONFIRM_MOVEMENT' ? 'Movimentações confirmadas e auditadas.' : 'Rejeição registrada.';
      } catch (error) { message.textContent = error.message; }
      finally { button.disabled = false; }
    };
    confirm.addEventListener('click', () => act('CONFIRM_MOVEMENT', confirm));
    reject.addEventListener('click', () => act('REJECT_MOVEMENT', reject));
    content.append(create('h3', { text: 'Movimentações disponíveis para estorno' }), choices, note,
      create('div', { className: 'decision-actions' }, [reject, confirm]), message);
  } else {
    content.append(create('p', { className: 'detail-empty', text: 'As notas de saída foram pesquisadas, mas nenhuma movimentação SISDEV vinculada está disponível para estorno.' }));
  }
  return content;
}

function renderRegularizationDetail(detail) {
  const document = detail.document || {};
  const isEntry = document.direcao === 'Entrada';
  $('regularization-detail-title').textContent = `NF ${safeText(document.numero_nfe)} · Série ${safeText(document.serie)}`;
  const content = $('regularization-detail-content');
  const sections = [
    create('section', { className: 'detail-hero' }, [
      create('div', { className: 'detail-status-line' }, [
        create('span', { className: `priority-badge priority-${String(document.prioridade || '').toLowerCase()}`, text: `Prioridade ${safeText(document.prioridade)}` }),
        create('span', { className: `tag ${recordStatusClass(document.situacao)}`, text: safeText(document.situacao) }),
      ]),
      create('p', { className: 'detail-diagnosis', text: document.diagnostico_situacao }),
      create('p', { className: 'detail-action', text: `Ação recomendada: ${safeText(document.acao_recomendada)}` }),
      detailGrid([
        ['Data', displayValue('data_documento', document.data_documento)], ['CNPJ', document.cnpj],
        ['Centro', document.centro], ['Direção', document.direcao], ['Itens/Lotes', document.itens_resumo],
        ['Emitente', document.emitente], ['Tipo de entrada', document.tipo_entrada],
        ['Status saldo', document.status_saldo], ['Status SAP', document.status_lancamento_sap],
        ['Status operacional', document.status_operacional], ['Status conciliação', document.status_conciliacao],
      ]),
    ]),
    traceSection('Produtos e lotes', detailTable(detail.items, [
      ['produto', 'Produto'], ['lote', 'Lote'], ['quantidade_sap', 'Quantidade'], ['unidade_sap', 'Unidade'],
      ['volume_embalagem', 'Volume embalagem'], ['quantidade_embalagem', 'Qnt. embalagem'],
    ]), true, `${(detail.items || []).length} itens`),
  ];
  if (!isEntry) sections.push(
    traceSection('Receitas possíveis', recipeSelectionPanel(detail), true, `${(detail.recipes || []).length}`),
  );
  sections.push(
    traceSection('SAP', detailTable(detail.sap, [
      ['numero_nfe', 'NF'], ['serie', 'Série'], ['data_documento', 'Data'], ['produto', 'Material'],
      ['lote_sap', 'Lote SAP'], ['lote', 'Lote fabricante'], ['quantidade_sap', 'Quantidade'],
      ['status_lancamento_sap', 'Status lançamento'],
    ]), false, `${(detail.sap || []).length}`),
  );
  if (!isEntry) sections.push(
    traceSection('SISDEV', detailTable(detail.sisdev, [
      ['id', 'Movimentação'], ['nf', 'NF'], ['movement_date', 'Data'], ['product', 'Produto'],
      ['lot', 'Lote'], ['quantity', 'Embalagens'], ['volume', 'Volume'], ['status', 'Situação'],
    ]), false, `${(detail.sisdev || []).length}`),
  );
  sections.push(
    traceSection('Conciliação', detailTable(detail.reconciliation, [
      ['status', 'Status'], ['diagnosis', 'Diagnóstico'], ['confidence', 'Confiança'], ['details', 'Regras/detalhes'],
    ]), false, `${(detail.reconciliation || []).length}`),
  );
  if (!isEntry) sections.push(
    traceSection('Saldo', detailTable(detail.balance, [
      ['produto', 'Produto'], ['lote', 'Lote'], ['saldo_disponivel', 'Saldo atual'],
      ['saldo_necessario', 'Necessário'], ['saldo_apos_lancamento', 'Após operação'],
      ['falta_saldo', 'Diferença'], ['status_saldo_codigo', 'Status'],
    ]), false, `${(detail.balance || []).length}`),
  );
  if (isEntry) sections.push(
    traceSection(document.tipo_entrada === 'TRANSFERENCIA_RETORNO' ? 'Retorno ao armazém / estorno' : 'Entrada de fornecedor', returnChainPanel(detail), true),
  );
  sections.push(
    traceSection('Histórico e auditoria', detailTable(detail.history, [
      ['data_hora', 'Data/hora'], ['usuario', 'Usuário'], ['action', 'Ação'], ['result', 'Resultado'], ['justificativa', 'Justificativa'],
    ]), false, `${(detail.history || []).length}`),
  );
  content.replaceChildren(...sections);
}

async function openRegularizationDetail(row) {
  const dialog = $('regularization-detail-dialog');
  const requestId = ++state.detailSequence;
  $('regularization-detail-title').textContent = `NF ${safeText(row.numero_nfe)}`;
  $('regularization-detail-content').replaceChildren(create('p', { className: 'detail-loading', text: 'Carregando a árvore de rastreabilidade desta NF...' }));
  if (!dialog.open) dialog.showModal();
  try {
    const detail = await requestJson(`/api/regularization/${encodeURIComponent(row.document_id)}?${qs()}`, { method: 'GET' }, 45_000);
    if (requestId !== state.detailSequence || !dialog.open) return;
    renderRegularizationDetail(detail);
  } catch (error) {
    if (requestId !== state.detailSequence) return;
    $('regularization-detail-content').replaceChildren(create('p', { className: 'table-error', text: error.message }));
  }
}

function renderPagination(page, pagination) {
  const container = $('page-pagination');
  container.replaceChildren();
  if (!pagination) {
    container.hidden = true;
    return;
  }
  container.hidden = false;
  const label = create('span', { text: `Mostrando ${formatNumber(pagination.from)}–${formatNumber(pagination.to)} de ${formatNumber(pagination.total)} registros` });
  const size = create('select', { attrs: { 'aria-label': 'Registros por página' } });
  for (const value of [25, 50, 100, 250]) size.append(new Option(`${value} por página`, String(value), false, Number(pagination.per_page) === value));
  size.addEventListener('change', () => {
    state.perPage = Number(size.value);
    state.page = 1;
    loadPage(page);
  });
  const previous = create('button', { type: 'button', className: 'secondary', text: 'Anterior', disabled: pagination.page <= 1 });
  const next = create('button', { type: 'button', className: 'secondary', text: 'Próxima', disabled: pagination.page >= pagination.total_pages });
  previous.addEventListener('click', () => { state.page = Math.max(1, pagination.page - 1); loadPage(page); });
  next.addEventListener('click', () => { state.page = pagination.page + 1; loadPage(page); });
  container.append(label, create('div', { className: 'pagination-controls' }, [size, previous, create('span', { text: `Página ${pagination.page} de ${pagination.total_pages || 1}` }), next]));
}

function renderPageSummary(page, summary) {
  const box = $('page-summary');
  box.replaceChildren();
  if (!['regularization', 'work_queue'].includes(page) || !summary) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  if (page === 'work_queue') {
    box.append(
      create('strong', { text: `${formatNumber(summary.total)} tarefas no ciclo atual` }),
      document.createTextNode(` · ${formatNumber(summary.novas)} novas · ${formatNumber(summary.em_analise)} em análise · ${formatNumber(summary.aguardando)} aguardando informação · ${formatNumber(summary.vencidas)} vencidas · ${formatNumber(summary.vence_hoje)} vencem hoje · ${formatNumber(summary.sem_responsavel)} sem responsável.`),
    );
  } else {
    box.append(
      create('strong', { text: `${formatNumber(summary.total)} notas para regularizar` }),
      document.createTextNode(` · ${formatNumber(summary.entries)} entradas · ${formatNumber(summary.exits)} saídas · ${formatNumber(summary.recipes_suggested)} com receita sugerida (D ou D-1).`),
    );
  }
}

function addExportButtons(container, page) {
  if (!['reports', 'regularization', 'pending', 'work_queue'].includes(page)) return;
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
  if (page === 'work_queue') {
    const priority = create('select', { attrs: { 'aria-label': 'Filtrar por prioridade' } });
    priority.append(new Option('Todas as prioridades', ''));
    for (const value of ['ALTA', 'MEDIA', 'BAIXA']) priority.append(new Option(formatLabel(value), value, false, state.workQueuePriority === value));
    priority.addEventListener('change', () => { state.workQueuePriority = priority.value; state.page = 1; loadPage(page); });

    const assignee = create('select', { attrs: { 'aria-label': 'Filtrar por responsável' } });
    assignee.append(new Option('Todos os responsáveis', ''), new Option('Sem responsável', '__unassigned__'));
    for (const user of state.workQueueAssignees) assignee.append(new Option(user.display_name, String(user.id), false, state.workQueueAssignee === String(user.id)));
    if (state.workQueueAssignee === '__unassigned__') assignee.value = '__unassigned__';
    assignee.addEventListener('change', () => { state.workQueueAssignee = assignee.value; state.page = 1; loadPage(page); });

    const due = create('select', { attrs: { 'aria-label': 'Filtrar por prazo' } });
    for (const [value, label] of [['', 'Todos os prazos'], ['ATRASADA', 'Atrasadas'], ['VENCE_HOJE', 'Vencem hoje'], ['NO_PRAZO', 'No prazo'], ['SEM_PRAZO', 'Sem prazo'], ['CONCLUIDA', 'Concluídas']]) {
      due.append(new Option(label, value, false, state.workQueueDueStatus === value));
    }
    due.addEventListener('change', () => { state.workQueueDueStatus = due.value; state.page = 1; loadPage(page); });

    const mine = create('button', { type: 'button', className: 'secondary', text: 'Minhas tarefas' });
    mine.addEventListener('click', () => {
      state.workQueueAssignee = String(state.auth?.user?.id || '');
      state.page = 1;
      loadPage(page);
    });
    actions.prepend(create('div', { className: 'work-queue-filters' }, [priority, assignee, due, mine]));
  }
  if (page === 'regularization') {
    const sortLabel = create('label', { className: 'queue-sort' }, create('span', { text: 'Ordenar por' }));
    const sortSelect = create('select', { attrs: { 'aria-label': 'Ordenar fila por' } });
    for (const [value, label] of [
      ['prioridade', 'Prioridade'], ['data', 'Data'], ['nf', 'NF'], ['status', 'Status'],
      ['compatibilidade', 'Compatibilidade'], ['centro', 'Centro'],
    ]) sortSelect.append(new Option(label, value, false, (state.sort || 'prioridade') === value));
    sortSelect.addEventListener('change', () => { state.sort = sortSelect.value; state.page = 1; loadPage(page); });
    sortLabel.append(sortSelect);
    const currentDirection = $('direction').value;
    const entryActive = currentDirection === '1';
    const exitActive = currentDirection === '2';
    const entry = create('button', {
      type: 'button',
      className: `secondary operation-toggle${entryActive ? ' active' : ''}`,
      text: 'Regularizar entrada',
      attrs: { 'aria-pressed': String(entryActive) },
    });
    const exit = create('button', {
      type: 'button',
      className: `secondary operation-toggle${exitActive ? ' active' : ''}`,
      text: 'Regularizar saída',
      attrs: { 'aria-pressed': String(exitActive) },
    });
    const toggleDirection = (value) => {
      $('direction').value = $('direction').value === value ? '' : value;
      state.page = 1;
      loadPage(page);
    };
    entry.addEventListener('click', () => toggleDirection('1'));
    exit.addEventListener('click', () => toggleDirection('2'));
    actions.prepend(sortLabel, entry, exit);
    addRtPreference(actions);
  }
  if (page === 'users') {
    const add = create('button', { type: 'button', text: 'Novo usuário' });
    add.addEventListener('click', () => openUserDialog());
    actions.append(add);
  }
}

function openUserDialog(user = null) {
  state.editingUserId = user?.id ?? null;
  $('user-form').reset();
  $('user-dialog-title').textContent = user ? 'Editar usuário' : 'Novo usuário';
  $('user-submit').textContent = user ? 'Salvar alterações' : 'Criar usuário';
  $('user-password-label').textContent = user ? 'Nova senha (opcional)' : 'Senha temporária';
  $('user-password').required = !user;
  $('user-form-message').textContent = '';
  $('user-name').value = user?.display_name || '';
  $('user-email').value = user?.email || '';
  $('user-profile').value = user?.profile || 'GESTOR';
  $('user-active').checked = user ? Boolean(user.active) : true;
  $('user-centers').value = (user?.scopes || [])
    .filter((scope) => scope.scope_type === 'CENTER')
    .map((scope) => scope.scope_value)
    .join(', ');
  $('user-dialog').showModal();
}

async function openWorkItem(row) {
  state.editingWorkItem = row;
  const canManage = hasAccess(state.auth?.user?.permissions) || (state.auth?.user?.permissions || []).includes('manage_work_queue');
  $('work-item-title').textContent = `Tarefa · NF ${safeText(row.numero_nfe)}`;
  $('work-item-context').textContent = `${safeText(row.situacao)} · ${safeText(row.centro)} · ${safeText(row.direcao)}`;
  $('work-item-status').value = row.andamento || 'NOVA';
  $('work-item-priority').value = row.prioridade_trabalho || 'MEDIA';
  $('work-item-due').value = row.prazo || '';
  $('work-item-comment').value = '';
  $('work-item-message').textContent = '';
  const assignee = $('work-item-assignee');
  assignee.replaceChildren(new Option('Não atribuído', ''));
  for (const user of state.workQueueAssignees) assignee.append(new Option(`${user.display_name} · ${formatLabel(user.profile)}`, String(user.id)));
  assignee.value = row.responsavel_id ? String(row.responsavel_id) : '';
  for (const id of ['work-item-assignee', 'work-item-priority', 'work-item-due']) $(id).disabled = !canManage;
  $('work-comments').replaceChildren(create('p', { text: 'Carregando histórico...' }));
  $('work-item-dialog').showModal();
  try {
    const data = await requestJson(`/api/work-items/${encodeURIComponent(row.document_id)}/comments`, { method: 'GET' }, 20_000);
    if (state.editingWorkItem?.document_id !== row.document_id) return;
    $('work-comments').replaceChildren(...((data.rows || []).length
      ? data.rows.map((item) => create('article', {}, [
          create('strong', { text: item.author }),
          create('small', { text: displayValue('created_at', item.created_at) }),
          create('p', { text: item.comment }),
        ]))
      : [create('p', { text: 'Nenhum comentário registrado.' })]));
  } catch (error) {
    $('work-comments').replaceChildren(create('p', { className: 'table-error', text: error.message }));
  }
}

async function saveCurrentWorkItem(event) {
  event.preventDefault();
  const row = state.editingWorkItem;
  if (!row) return;
  const button = $('save-work-item');
  button.disabled = true;
  $('work-item-message').textContent = 'Salvando...';
  try {
    await requestJson(`/api/work-items/${encodeURIComponent(row.document_id)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        status: $('work-item-status').value,
        assigned_to: $('work-item-assignee').value || null,
        priority: $('work-item-priority').value,
        due_date: $('work-item-due').value || null,
      }),
    }, 20_000);
    const comment = $('work-item-comment').value.trim();
    if (comment) await requestJson(`/api/work-items/${encodeURIComponent(row.document_id)}/comments`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ comment }),
    }, 20_000);
    $('work-item-dialog').close();
    state.editingWorkItem = null;
    await loadPage('work_queue');
  } catch (error) {
    $('work-item-message').textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function deleteExistingUser(user, button) {
  if (Number(user.id) === Number(state.auth?.user?.id)) {
    $('notice').hidden = false;
    $('notice').textContent = 'Não é possível excluir o próprio usuário.';
    return;
  }
  if (!window.confirm(`Excluir o acesso de ${user.display_name}? A trilha de auditoria será preservada.`)) return;
  button.disabled = true;
  try {
    await requestJson(`/api/auth/users/${user.id}`, {
      method: 'DELETE', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ justification: 'Exclusão solicitada no painel administrativo.' }),
    }, 20_000);
    $('notice').hidden = false;
    $('notice').textContent = `Usuário ${user.display_name} excluído com segurança.`;
    await loadPage('users');
  } catch (error) {
    $('notice').hidden = false;
    $('notice').textContent = error.message;
    button.disabled = false;
  }
}

async function loadPage(page) {
  const requestId = ++state.requestSequence;
  const config = PAGE_CONFIG[page] || { title: formatLabel(page), description: '' };
  $('page-heading').textContent = config.title;
  $('page-description').textContent = config.description;
  $('page-head').replaceChildren();
  $('page-body').replaceChildren(create('tr', {}, create('td', { text: 'Carregando registros...' })));
  try {
    const endpoint = page === 'work_queue' ? '/api/work-items' : `/api/page/${encodeURIComponent(page)}`;
    const data = await requestJson(`${endpoint}?${qs()}`, { method: 'GET' }, 45_000);
    if (requestId !== state.requestSequence || state.current !== page) return;
    if (page === 'work_queue') state.workQueueAssignees = data.assignees || [];
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
  $('import-health-view').hidden = page !== 'import_health';
  $('lot-trace-view').hidden = page !== 'lot_trace';
  $('dashboard-view').hidden = page !== 'dashboard';
  $('page-view').hidden = ['dashboard', 'uploads', 'import_health', 'lot_trace'].includes(page);
  $('filters').hidden = ['uploads', 'import_health', 'lot_trace'].includes(page);
  if (page !== 'import_health') window.clearTimeout(state.importHealthTimer);
  if (page !== 'dashboard') $('notice').hidden = true;
}

function configureFilters(page) {
  const operational = ['pending', 'regularization', 'work_queue'].includes(page);
  $('direction-filter-label').hidden = page === 'regularization';
  $('status-filter-label').hidden = !operational;
  $('nf-filter-label').hidden = !operational;
  $('lot-filter-label').hidden = page !== 'regularization';
  const select = $('status-filter');
  const selected = select.value;
  const options = page === 'pending'
    ? ['PENDENTE', 'REGULARIZADO', 'DIVERGENTE', 'SEM_RECEITA', 'RECEITAS_MULTIPLAS', 'SEM_SALDO', 'MATERIAL_NAO_MAPEADO', 'LOTE_NAO_MAPEADO', 'ERRO_CONCILIACAO']
    : page === 'regularization'
      ? ['PENDENTE', 'EM_ANALISE', 'TRATADO', 'ENTRADA_FORNECEDOR', 'OK', 'SEM_RECEITA', 'RECEITAS_MULTIPLAS', 'SALDO_PARCIAL', 'SEM_SALDO', 'MATERIAL_NAO_MAPEADO', 'LOTE_NAO_MAPEADO', 'MOVIMENTACAO_NAO_LOCALIZADA', 'PENDENTE_ESTORNO', 'ESTORNO_PARCIAL', 'ESTORNO_COMPLETO', 'DIVERGENTE', 'ERRO_CONCILIACAO']
      : page === 'work_queue'
        ? ['NOVA', 'EM_ANALISE', 'AGUARDANDO_INFORMACAO', 'REGULARIZADA', 'VALIDADA']
      : [];
  select.replaceChildren(new Option('Todos', ''));
  for (const value of options) select.append(new Option(formatLabel(value), value));
  if (options.includes(selected)) select.value = selected;
}

async function navigate(page, updateHistory = true) {
  const validPage = ['dashboard', 'uploads', 'import_health', 'lot_trace'].includes(page) || PAGE_CONFIG[page] ? page : 'dashboard';
  state.current = validPage;
  state.page = 1;
  state.sort = '';
  state.order = 'asc';
  setActiveNavigation(validPage);
  updateViewVisibility(validPage);
  configureFilters(validPage);
  const config = PAGE_CONFIG[validPage];
  $('title').textContent = validPage === 'dashboard' ? 'Dashboard' : validPage === 'uploads' ? 'Importar arquivos' : validPage === 'import_health' ? 'Saúde das importações' : validPage === 'lot_trace' ? 'Rastreabilidade de lote' : config.title;
  $('subtitle').textContent = validPage === 'dashboard'
    ? 'Visão geral da auditoria e conciliação de movimentações de químicos'
    : validPage === 'uploads'
      ? 'Fluxo controlado de envio, processamento e conciliação por fonte'
      : validPage === 'import_health'
        ? 'Visão operacional do ciclo atual e das fontes que exigem atenção'
      : validPage === 'lot_trace'
        ? 'Linha cronológica de documentos e movimentos por produto e lote'
      : config.description;
  if (updateHistory && window.location.hash !== `#${validPage}`) history.pushState({ page: validPage }, '', `#${validPage}`);
  if (validPage === 'dashboard') await loadDashboard();
  else if (validPage === 'uploads') {
    renderUploadSummary();
    for (const source of UPLOAD_SOURCES) renderJob(source.id);
  } else if (validPage === 'import_health') await loadImportHealth();
  else if (validPage === 'lot_trace') {
    await loadLotTraceOptions($('lot-trace-search').value.trim());
    if (state.lotTraceSelection) await searchLotTrace(false);
  }
  else await loadPage(validPage);
}

function hasAccess(value) {
  const values = new Set(value || []);
  return values.has('*');
}

function applyAccess() {
  const user = state.auth?.user;
  if (!user) return;
  const modules = new Set(user.modules || []);
  const permissions = new Set(user.permissions || []);
  document.body.dataset.profile = user.profile;
  for (const link of document.querySelectorAll('nav a[data-page]')) {
    const page = link.dataset.page;
    let allowed = modules.has('*') || modules.has(page);
    if (page === 'uploads') allowed = permissions.has('*') || permissions.has('import');
    if (page === 'import_health') allowed = permissions.has('*') || permissions.has('import');
    if (page === 'users') allowed = permissions.has('*') || permissions.has('manage_users');
    link.hidden = !allowed;
  }
  $('current-user').textContent = `${user.display_name} · ${formatLabel(user.profile)}`;
}

function showLogin(message = '') {
  state.auth = null;
  document.body.classList.remove('authenticated');
  $('login-message').textContent = message;
  $('login-password').value = '';
}

async function activateSession(payload) {
  state.auth = { user: payload.user, csrfToken: payload.csrf_token };
  document.body.classList.add('authenticated');
  $('login-message').textContent = '';
  applyAccess();
  if (!state.appInitialized) {
    state.appInitialized = true;
    buildUploadArea();
    installEvents();
  }
  await restoreRemoteJobs();
  await navigate(window.location.hash.slice(1) || 'dashboard', false);
}

async function restoreAuth() {
  try {
    const payload = await requestJson('/api/auth/me', { method: 'GET' }, 20_000);
    await activateSession(payload);
    return true;
  } catch (error) {
    showLogin(error.status === 401 ? '' : error.message);
    return false;
  }
}

function installAuthEvents() {
  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('login-submit');
    button.disabled = true;
    $('login-message').textContent = 'Autenticando...';
    try {
      const payload = await requestJson('/api/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: $('login-email').value, password: $('login-password').value }),
      }, 20_000);
      await activateSession(payload);
    } catch (error) {
      $('login-message').textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
  $('logout').addEventListener('click', async () => {
    try { await requestJson('/api/auth/logout', { method: 'POST' }, 20_000); } catch { /* sessão já inválida */ }
    showLogin('Sessão encerrada com segurança.');
  });
  $('cancel-user').addEventListener('click', () => $('user-dialog').close());
  $('user-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const centers = $('user-centers').value.split(',').map((value) => value.trim()).filter(Boolean);
    try {
      const payload = {
          display_name: $('user-name').value, email: $('user-email').value,
          profile: $('user-profile').value, active: $('user-active').checked,
          scopes: centers.map((scope_value) => ({ scope_type: 'CENTER', scope_value })),
      };
      if ($('user-password').value) payload.password = $('user-password').value;
      const editing = state.editingUserId !== null;
      await requestJson(editing ? `/api/auth/users/${state.editingUserId}` : '/api/auth/users', {
        method: editing ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }, 20_000);
      $('user-dialog').close();
      $('user-form').reset();
      state.editingUserId = null;
      if (state.current === 'users') await loadPage('users');
    } catch (error) {
      $('user-form-message').textContent = error.message;
    }
  });
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
    } else if (state.current === 'import_health') {
      await loadImportHealth();
    } else if (state.current === 'lot_trace') {
      await searchLotTrace(false);
    } else if (state.current === 'dashboard') await loadDashboard();
    else await loadPage(state.current);
  } finally {
    button.disabled = false;
    button.textContent = '↻ Atualizar dados';
  }
}

function installEvents() {
  $('close-regularization-detail').addEventListener('click', () => {
    state.detailSequence += 1;
    $('regularization-detail-dialog').close();
  });
  $('regularization-detail-dialog').addEventListener('cancel', () => { state.detailSequence += 1; });
  $('cancel-work-item').addEventListener('click', () => { state.editingWorkItem = null; $('work-item-dialog').close(); });
  $('work-item-form').addEventListener('submit', saveCurrentWorkItem);
  for (const link of document.querySelectorAll('nav a[data-page]')) {
    link.addEventListener('click', (event) => {
      event.preventDefault();
      navigate(link.dataset.page);
    });
  }
  $('apply').addEventListener('click', () => {
    state.page = 1;
    return state.current === 'dashboard' ? loadDashboard() : loadPage(state.current);
  });
  $('clear-filters').addEventListener('click', () => {
    for (const id of ['from', 'to', 'center', 'direction', 'status-filter', 'nf-filter', 'lot-filter']) $(id).value = '';
    if (state.current === 'work_queue') {
      state.workQueuePriority = '';
      state.workQueueAssignee = '';
      state.workQueueDueStatus = '';
    }
    state.page = 1;
    return state.current === 'dashboard' ? loadDashboard() : loadPage(state.current);
  });
  $('refresh-data').addEventListener('click', refreshCurrentView);
  $('process-upload').addEventListener('click', reconcileCompleted);
  $('refresh-import-health').addEventListener('click', loadImportHealth);
  $('open-uploads').addEventListener('click', () => navigate('uploads'));
  $('search-lot-trace').addEventListener('click', () => searchLotTrace(true));
  $('lot-trace-search').addEventListener('input', () => {
    window.clearTimeout(state.lotTraceSearchTimer);
    state.lotTraceSearchTimer = window.setTimeout(() => loadLotTraceOptions($('lot-trace-search').value.trim()), 250);
  });
  $('lot-trace-search').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); searchLotTrace(true); }
  });
  window.addEventListener('popstate', () => navigate(window.location.hash.slice(1) || 'dashboard', false));
}

async function init() {
  installAuthEvents();
  await restoreAuth();
}

init();
