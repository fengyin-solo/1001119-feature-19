<template>
  <section class="page auth-page" data-module="auth_cabinet">
    <header class="page-head">
      <div>
        <h2>项目领用授权柜</h2>
        <p class="page-desc">
          按工程 × 施工路段 × 材料类别给班组授予「领用 / 只读」权限；默认继承工程归属，
          紧急抢险走指挥审批临时例外。领料越权一律拒绝且不暴露供应商，授权结论随流程落到
          材料台账、工程待办与车辆装载清单。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">生效授权</span>
        <strong class="stat-value">{{ activeGrants }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待指挥审批例外</span>
        <strong class="stat-value">{{ pendingExceptions }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待处理工程待办</span>
        <strong class="stat-value">{{ pendingTodos }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">预占中领料单</span>
        <strong class="stat-value">{{ reservedCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">缓存版本 / 事务序号</span>
        <strong class="stat-value">v{{ cacheVersion }} · #{{ cacheTxn }}</strong>
      </article>
    </div>

    <nav class="tabs">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <p v-if="message" class="notice" :class="{ error: !!errorMessage }">
      {{ errorMessage || message }}
    </p>

    <!-- 实时预览 -->
    <div v-if="activeTab === 'preview'" class="panel">
      <h3>授权实时预览</h3>
      <p class="hint">选择工程、材料类别与领料班组，实时查看授权结论，以及会被影响的工程待办、库存与在途单据。</p>
      <form class="filter-bar" @submit.prevent="loadPreview">
        <label class="filter-item">
          <span>工程（含施工路段）</span>
          <select v-model="previewForm.project_id">
            <option value="" disabled>请选择工程</option>
            <option v-for="p in meta.projects" :key="p.id" :value="p.id">
              {{ p.工程编号 }}｜{{ p.工程名称 }}（{{ p.施工路段 }}）
            </option>
          </select>
        </label>
        <label class="filter-item">
          <span>材料类别</span>
          <select v-model="previewForm.category">
            <option value="" disabled>请选择类别</option>
            <option v-for="c in meta.categories" :key="c" :value="c">{{ c }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>领料班组</span>
          <select v-model="previewForm.team_id">
            <option value="" disabled>请选择班组</option>
            <option v-for="t in meta.teams" :key="t.id" :value="t.id">
              {{ t.班组名称 }}（{{ t.归属单位 }}）
            </option>
          </select>
        </label>
        <button class="btn primary" type="submit">实时预览</button>
      </form>

      <template v-if="preview">
        <div class="decision" :class="decisionClass">
          <div>
            <strong>授权结论：{{ preview.decision.权限 }}</strong>
            <span class="decision-source">来源：{{ preview.decision.来源 }}</span>
            <span class="decision-note">{{ preview.decision.说明 }}</span>
          </div>
          <div class="quota">
            <span>授权额度 <b>{{ preview.decision.授权额度 }}</b></span>
            <span>已预占 <b>{{ preview.decision.已预占 }}</b></span>
            <span>已领用 <b>{{ preview.decision.已领用 }}</b></span>
            <span>剩余额度 <b>{{ preview.decision.剩余额度 }}</b></span>
          </div>
        </div>
        <p class="hint" v-if="preview.decision.有效期止">
          紧急例外有效期至 {{ preview.decision.有效期止 }}，到期自动失效。
        </p>

        <h4>会影响的库存（{{ preview.stock.length }} 项）
          <span class="tag" :class="preview.供应商可见 ? 'ok' : 'blocked'">
            {{ preview.供应商可见 ? '供应商可见' : '未授权：供应商已隐藏' }}
          </span>
        </h4>
        <table class="data-table">
          <thead>
            <tr>
              <th>材料编号</th><th>材料名称</th><th>规格型号</th><th>计量单位</th>
              <th>库存总量</th><th>已预占</th><th>已领用</th><th>可用库存</th>
              <th>供应商</th><th>存放地点</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in preview.stock" :key="s.材料编号">
              <td>{{ s.材料编号 }}</td><td>{{ s.材料名称 }}</td><td>{{ s.规格型号 }}</td>
              <td>{{ s.计量单位 }}</td><td>{{ s.库存总量 }}</td><td>{{ s.已预占 }}</td>
              <td>{{ s.已领用 }}</td><td>{{ s.可用库存 }}</td>
              <td>{{ s.供应商 ?? '— 无权限查看 —' }}</td><td>{{ s.存放地点 }}</td>
            </tr>
          </tbody>
        </table>

        <h4>会影响的工程待办（{{ preview.tasks.length }} 条）</h4>
        <table class="data-table">
          <thead><tr><th>类型</th><th>标题</th><th>授权结论</th><th>关联单据</th><th>创建时间</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="t in preview.tasks" :key="t.id">
              <td>{{ t.类型 }}</td><td>{{ t.标题 }}</td>
              <td><span class="tag" :class="conclusionTag(t.授权结论)">{{ t.授权结论 }}</span></td>
              <td>{{ t.关联单据 }}</td><td>{{ t.创建时间 }}</td><td>{{ t.状态值 }}</td>
            </tr>
            <tr v-if="!preview.tasks.length"><td colspan="6" class="empty-state">该工程暂无待办</td></tr>
          </tbody>
        </table>

        <h4>在途/历史领料单（{{ preview.requisitions.length }} 张）</h4>
        <table class="data-table">
          <thead><tr><th>领料单号</th><th>材料</th><th>数量</th><th>班组</th><th>状态</th><th>授权结论</th><th>供应商</th></tr></thead>
          <tbody>
            <tr v-for="q in preview.requisitions" :key="q.id">
              <td>{{ q.领料单号 }}</td>
              <td>{{ q.材料名称 || q.材料类别 }} {{ q.规格型号 }}</td>
              <td>{{ q.数量 }}{{ q.计量单位 }}</td>
              <td>{{ q.领料班组 }}</td><td>{{ q.状态 }}</td>
              <td>{{ q.授权结论 }}</td>
              <td>{{ q.供应商 || '— 无权限查看 —' }}</td>
            </tr>
            <tr v-if="!preview.requisitions.length"><td colspan="7" class="empty-state">该工程/类别暂无领料单</td></tr>
          </tbody>
        </table>

        <h4>车辆装载清单（{{ preview.loads.length }} 条）</h4>
        <table class="data-table">
          <thead><tr><th>车辆</th><th>领料单号</th><th>材料</th><th>数量</th><th>授权结论</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="l in preview.loads" :key="l.id">
              <td>{{ l.车牌号 || l.车辆编号 }}</td><td>{{ l.领料单号 }}</td>
              <td>{{ l.材料名称 }} {{ l.规格型号 }}</td><td>{{ l.数量 }}{{ l.计量单位 }}</td>
              <td>{{ l.授权结论 }}</td><td>{{ l.状态值 }}</td>
            </tr>
            <tr v-if="!preview.loads.length"><td colspan="6" class="empty-state">暂无装载记录</td></tr>
          </tbody>
        </table>
      </template>
    </div>

    <!-- 授权配置 -->
    <div v-if="activeTab === 'grants'" class="panel">
      <h3>授权规则（工程 × 施工路段 × 材料类别）</h3>
      <form class="form-grid" @submit.prevent="submitGrant">
        <label class="filter-item">
          <span>工程</span>
          <select v-model="grantForm.工程id" required>
            <option value="" disabled>请选择工程</option>
            <option v-for="p in meta.projects" :key="p.id" :value="p.id">
              {{ p.工程编号 }}｜{{ p.工程名称 }}
            </option>
          </select>
        </label>
        <label class="filter-item">
          <span>被授权班组</span>
          <select v-model="grantForm.班组id" required>
            <option value="" disabled>请选择班组</option>
            <option v-for="t in meta.teams" :key="t.id" :value="t.id">
              {{ t.班组名称 }}（{{ t.归属单位 }}）
            </option>
          </select>
        </label>
        <label class="filter-item">
          <span>材料类别</span>
          <select v-model="grantForm.材料类别" required>
            <option value="" disabled>请选择类别</option>
            <option v-for="c in meta.categories" :key="c" :value="c">{{ c }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>权限</span>
          <select v-model="grantForm.权限" required>
            <option value="领用">领用</option>
            <option value="只读">只读（不可领料）</option>
          </select>
        </label>
        <label class="filter-item">
          <span>授权额度（只读时忽略）</span>
          <input v-model.number="grantForm.授权额度" type="number" min="1" placeholder="如 120" />
        </label>
        <label class="filter-item">
          <span>授权人</span>
          <input v-model="grantForm.授权人" placeholder="如 材料科 周敏" />
        </label>
        <div class="form-actions">
          <button class="btn primary" type="submit">授予权限</button>
        </div>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>工程</th><th>施工路段</th><th>材料类别</th><th>授权对象</th>
            <th>权限</th><th>额度</th><th>已预占</th><th>已领用</th>
            <th>来源</th><th>有效期止</th><th>状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="g in grants" :key="g.id">
            <td>{{ g.工程编号 }}</td><td>{{ g.施工路段 }}</td><td>{{ g.材料类别 }}</td>
            <td>{{ g.授权对象 }}</td>
            <td><span class="tag" :class="g.权限 === '领用' ? 'ok' : 'readonly'">{{ g.权限 }}</span></td>
            <td>{{ g.授权额度 }}</td><td>{{ g.已预占 }}</td><td>{{ g.已领用 }}</td>
            <td>{{ g.来源 }}</td><td>{{ g.有效期止 || '—' }}</td><td>{{ g.状态 }}</td>
            <td>
              <button v-if="g.状态 === '生效中'" class="link" type="button" @click="revokeGrant(g.id)">
                停用
              </button>
              <span v-else class="muted">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 紧急例外 -->
    <div v-if="activeTab === 'exceptions'" class="panel">
      <h3>紧急抢险临时例外（以指挥审批为准）</h3>
      <p class="hint">申请提交后进入「待指挥审批」，批准前不产生任何领料权限；批准后生成带有效期的临时领用授权。</p>
      <form class="form-grid" @submit.prevent="submitException">
        <label class="filter-item">
          <span>工程</span>
          <select v-model="exceptionForm.工程id" required>
            <option value="" disabled>请选择工程</option>
            <option v-for="p in meta.projects" :key="p.id" :value="p.id">
              {{ p.工程编号 }}｜{{ p.工程名称 }}（{{ p.施工路段 }}）
            </option>
          </select>
        </label>
        <label class="filter-item">
          <span>申请班组</span>
          <select v-model="exceptionForm.班组id" required>
            <option value="" disabled>请选择班组</option>
            <option v-for="t in meta.teams" :key="t.id" :value="t.id">{{ t.班组名称 }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>材料类别</span>
          <input v-model="exceptionForm.材料类别" list="cat-list" required placeholder="如 结构胶" />
        </label>
        <label class="filter-item">
          <span>申请额度</span>
          <input v-model.number="exceptionForm.申请额度" type="number" min="1" required />
        </label>
        <label class="filter-item wide">
          <span>抢险事由</span>
          <input v-model="exceptionForm.事由" required placeholder="如 K2+100管网坍塌，需立即植筋加固" />
        </label>
        <label class="filter-item">
          <span>申请人</span>
          <input v-model="exceptionForm.申请人" placeholder="班组长姓名" />
        </label>
        <datalist id="cat-list">
          <option v-for="c in meta.categories" :key="c" :value="c" />
        </datalist>
        <div class="form-actions"><button class="btn primary" type="submit">提交临时例外申请</button></div>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>申请单号</th><th>工程/路段</th><th>类别</th><th>申请班组</th>
            <th>额度</th><th>事由</th><th>状态</th><th>审批人/意见</th><th>有效期止</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in exceptions" :key="e.id">
            <td>{{ e.申请单号 }}</td><td>{{ e.工程编号 }}｜{{ e.施工路段 }}</td>
            <td>{{ e.材料类别 }}</td><td>{{ e.申请班组 }}</td>
            <td>{{ e.申请额度 }}</td><td class="reason-cell">{{ e.事由 }}</td>
            <td><span class="tag" :class="exceptionTag(e.状态)">{{ e.状态 }}</span></td>
            <td>{{ e.审批人 ? `${e.审批人}：${e.审批意见}` : '—' }}</td>
            <td>{{ e.有效期止 || '—' }}</td>
            <td class="row-actions">
              <template v-if="e.状态 === '待指挥审批'">
                <button class="link" type="button" @click="decideException(e.id, true)">指挥批准</button>
                <button class="link danger" type="button" @click="decideException(e.id, false)">驳回</button>
              </template>
              <span v-else class="muted">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 领料 -->
    <div v-if="activeTab === 'requisition'" class="panel">
      <h3>班组领料（越权拒绝 · 额度与库存原子预占）</h3>
      <form class="form-grid" @submit.prevent="submitRequisition">
        <label class="filter-item">
          <span>工程</span>
          <select v-model="requisitionForm.工程id" required>
            <option value="" disabled>请选择工程</option>
            <option v-for="p in meta.projects" :key="p.id" :value="p.id">
              {{ p.工程编号 }}｜{{ p.工程名称 }}
            </option>
          </select>
        </label>
        <label class="filter-item">
          <span>领料班组</span>
          <select v-model="requisitionForm.班组id" required>
            <option value="" disabled>请选择班组</option>
            <option v-for="t in meta.teams" :key="t.id" :value="t.id">{{ t.班组名称 }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>材料类别</span>
          <select v-model="requisitionForm.材料类别" required>
            <option value="" disabled>请选择类别</option>
            <option v-for="c in meta.categories" :key="c" :value="c">{{ c }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>数量</span>
          <input v-model.number="requisitionForm.数量" type="number" min="1" required />
        </label>
        <label class="filter-item">
          <span>领料人</span>
          <input v-model="requisitionForm.领料人" required placeholder="领用人姓名" />
        </label>
        <label class="filter-item">
          <span>装载车辆（可选）</span>
          <select v-model="requisitionForm.车辆id">
            <option :value="null">不指定</option>
            <option v-for="v in meta.vehicles" :key="v.id" :value="v.id">
              {{ v.车牌号 }}｜{{ v.车辆类型 }}
            </option>
          </select>
        </label>
        <div class="form-actions"><button class="btn primary" type="submit">提交领料</button></div>
      </form>

      <h4>领料单台账</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>领料单号</th><th>工程</th><th>材料</th><th>数量</th><th>班组/领料人</th>
            <th>车辆</th><th>状态</th><th>授权结论</th><th>供应商</th><th>时间</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="q in requisitions" :key="q.id" :class="{ rejected: q.状态 === '已拒绝' }">
            <td>{{ q.领料单号 }}</td><td>{{ q.工程编号 }}</td>
            <td>{{ q.材料名称 || q.材料类别 }} {{ q.规格型号 }}</td>
            <td>{{ q.数量 }}{{ q.计量单位 }}</td>
            <td>{{ q.领料班组 }} / {{ q.领料人 }}</td>
            <td>{{ q.车牌号 || '—' }}</td>
            <td><span class="tag" :class="requisitionTag(q.状态)">{{ q.状态 }}</span></td>
            <td class="conclusion-cell">{{ q.授权结论 }}</td>
            <td>{{ q.供应商 || '— 已隐藏 —' }}</td>
            <td>{{ q.出库时间 || q.申请时间 }}</td>
            <td class="row-actions">
              <button v-if="q.状态 === '已预占待出库'" class="link" type="button" @click="outbound(q.id)">
                确认出库
              </button>
              <button v-if="q.状态 === '已预占待出库'" class="link danger" type="button" @click="cancel(q.id)">
                取消
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 流程落点 -->
    <div v-if="activeTab === 'downstream'" class="panel">
      <h3>授权结论落点</h3>
      <h4>工程待办</h4>
      <table class="data-table">
        <thead><tr><th>工程</th><th>类型</th><th>标题</th><th>授权结论</th><th>关联单据</th><th>创建时间</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="t in todos" :key="t.id">
            <td>{{ t.工程编号 }}</td><td>{{ t.类型 }}</td><td>{{ t.标题 }}</td>
            <td><span class="tag" :class="conclusionTag(t.授权结论)">{{ t.授权结论 }}</span></td>
            <td>{{ t.关联单据 }}</td><td>{{ t.创建时间 }}</td><td>{{ t.状态值 }}</td>
            <td>
              <button v-if="t.状态值 === '待处理'" class="link" type="button" @click="resolveTodo(t.id)">
                处理
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <h4>车辆装载清单</h4>
      <table class="data-table">
        <thead><tr><th>车辆</th><th>领料单号</th><th>工程/路段</th><th>材料</th><th>数量</th><th>授权结论</th><th>状态</th><th>装车时间</th></tr></thead>
        <tbody>
          <tr v-for="l in loads" :key="l.id">
            <td>{{ l.车牌号 || l.车辆编号 }}</td><td>{{ l.领料单号 }}</td>
            <td>{{ l.工程名称 }}｜{{ l.施工路段 }}</td>
            <td>{{ l.材料名称 }} {{ l.规格型号 }}</td><td>{{ l.数量 }}{{ l.计量单位 }}</td>
            <td class="conclusion-cell">{{ l.授权结论 }}</td><td>{{ l.状态值 }}</td><td>{{ l.装车时间 || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <h4>权限缓存与工程转组</h4>
      <p class="hint">
        工程转组只重算「工程归属继承」口径，历史领料单保留工程名称与承建单位快照，不因转组改写。
      </p>
      <form class="filter-bar" @submit.prevent="transfer">
        <label class="filter-item">
          <span>选择工程</span>
          <select v-model="transferForm.project_id">
            <option value="" disabled>请选择工程</option>
            <option v-for="p in meta.projects" :key="p.id" :value="p.id">
              {{ p.工程编号 }}｜{{ p.工程名称 }}（当前：{{ p.承建单位 }}）
            </option>
          </select>
        </label>
        <label class="filter-item">
          <span>新承建单位</span>
          <input v-model="transferForm.新承建单位" placeholder="如 桥隧养护中心" />
        </label>
        <button class="btn" type="submit">执行转组并重算缓存</button>
        <button class="btn ghost" type="button" @click="loadCache">刷新缓存快照</button>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

const ENDPOINT = '/api/auth-cabinet'

type Json = Record<string, any>

const tabs = [
  { key: 'preview', label: '实时预览' },
  { key: 'grants', label: '授权规则' },
  { key: 'exceptions', label: '紧急例外' },
  { key: 'requisition', label: '班组领料' },
  { key: 'downstream', label: '结论落点与缓存' },
]
const activeTab = ref('preview')

const meta = ref<Json>({ projects: [], teams: [], categories: [], vehicles: [] })
const grants = ref<Json[]>([])
const exceptions = ref<Json[]>([])
const requisitions = ref<Json[]>([])
const todos = ref<Json[]>([])
const loads = ref<Json[]>([])
const preview = ref<Json | null>(null)
const cacheVersion = ref(0)
const cacheTxn = ref(0)
const message = ref('')
const errorMessage = ref('')

const previewForm = reactive({ project_id: '', category: '', team_id: '' })
const grantForm = reactive({ 工程id: '', 班组id: '', 材料类别: '', 权限: '领用', 授权额度: 100, 授权人: '材料科 周敏' })
const exceptionForm = reactive({ 工程id: '', 班组id: '', 材料类别: '', 申请额度: 10, 事由: '', 申请人: '' })
const requisitionForm = reactive({ 工程id: '', 班组id: '', 材料类别: '', 数量: 1, 领料人: '', 车辆id: null as number | null })
const transferForm = reactive({ project_id: '', 新承建单位: '' })

const activeGrants = computed(() => grants.value.filter((g) => g.状态 === '生效中').length)
const pendingExceptions = computed(() => exceptions.value.filter((e) => e.状态 === '待指挥审批').length)
const pendingTodos = computed(() => todos.value.filter((t) => t.状态值 === '待处理').length)
const reservedCount = computed(() => requisitions.value.filter((q) => q.状态 === '已预占待出库').length)
const decisionClass = computed(() => {
  const permission = preview.value?.decision?.权限
  return permission === '领用' ? 'allow' : permission === '只读' ? 'readonly-decision' : 'deny'
})

function notify(text: string, isError = false) {
  message.value = isError ? '' : text
  errorMessage.value = isError ? text : ''
  window.setTimeout(() => {
    message.value = ''
    errorMessage.value = ''
  }, 5000)
}

async function callApi(path: string, init?: RequestInit, silent = false): Promise<any> {
  const response = await request(`${ENDPOINT}${path}`, init)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = payload?.detail
    const text = detail?.message || (typeof detail === 'string' ? detail : '请求被服务端拒绝')
    if (!silent) notify(text, true)
    const error = new Error(text) as Error & { code?: string }
    error.code = detail?.code
    throw error
  }
  return payload
}

async function loadMeta() {
  meta.value = await callApi('/meta', undefined, true)
}

async function loadAll() {
  const [g, e, q, t, l] = await Promise.all([
    callApi('/grants', undefined, true),
    callApi('/exceptions', undefined, true),
    callApi('/requisitions', undefined, true),
    callApi('/todos', undefined, true),
    callApi('/loads', undefined, true),
  ])
  grants.value = g.items
  exceptions.value = e.items
  requisitions.value = q.items
  todos.value = t.items
  loads.value = l.items
  await loadCache()
}

async function loadCache() {
  const snap = await callApi('/cache', undefined, true)
  cacheVersion.value = snap.缓存版本
  cacheTxn.value = snap.事务序号
}

async function loadPreview() {
  if (!previewForm.project_id || !previewForm.category || !previewForm.team_id) {
    notify('请先选齐工程、材料类别与班组', true)
    return
  }
  const query = new URLSearchParams({
    project_id: String(previewForm.project_id),
    category: previewForm.category,
    team_id: String(previewForm.team_id),
  })
  preview.value = await callApi(`/preview?${query.toString()}`)
}

async function submitGrant() {
  await callApi('/grants', {
    method: 'POST',
    body: JSON.stringify({ values: { ...grantForm } }),
  })
  notify('授权已生效，工程待办已同步')
  await loadAll()
}

async function revokeGrant(id: number) {
  await callApi(`/grants/${id}/revoke`, {
    method: 'POST',
    body: JSON.stringify({ values: { 操作人: '值班管理员' } }),
  })
  notify('授权已停用')
  await loadAll()
}

async function submitException() {
  await callApi('/exceptions', {
    method: 'POST',
    body: JSON.stringify({ values: { ...exceptionForm } }),
  })
  notify('临时例外已提交，等待指挥审批')
  exceptionForm.事由 = ''
  await loadAll()
}

async function decideException(id: number, approve: boolean) {
  const values = approve
    ? { 审批人: '总指挥 王磊', 审批意见: '同意紧急抢险领用，限期24小时', 有效期小时: 24 }
    : { 审批人: '副总指挥 赵刚', 审批意见: '事由不充分，不予临时授权' }
  await callApi(`/exceptions/${id}/${approve ? 'approve' : 'reject'}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  notify(approve ? '指挥已批准，临时授权生效' : '指挥已驳回，未产生授权')
  await loadAll()
}

async function submitRequisition() {
  try {
    await callApi('/requisitions', {
      method: 'POST',
      body: JSON.stringify({ values: { ...requisitionForm } }),
    })
    notify('授权通过，额度与库存已原子预占，待出库')
  } catch {
    // 越权/超额/缺货：拒绝原因已通过 notify 展示，拒绝单也已落台账
  }
  await loadAll()
}

async function outbound(id: number) {
  await callApi(`/requisitions/${id}/outbound`, { method: 'POST' })
  notify('已出库，授权结论已落入材料台账与车辆装载清单')
  await loadAll()
}

async function cancel(id: number) {
  await callApi(`/requisitions/${id}/cancel`, { method: 'POST' })
  notify('领料已取消，预占已释放')
  await loadAll()
}

async function resolveTodo(id: number) {
  await callApi(`/todos/${id}/resolve`, {
    method: 'POST',
    body: JSON.stringify({ values: { 处理人: '值班管理员' } }),
  })
  await loadAll()
}

async function transfer() {
  if (!transferForm.project_id || !transferForm.新承建单位) {
    notify('请选择工程并填写新承建单位', true)
    return
  }
  const result = await callApi(`/projects/${transferForm.project_id}/transfer`, {
    method: 'POST',
    body: JSON.stringify({ values: { 新承建单位: transferForm.新承建单位 } }),
  })
  notify(`转组完成：缓存 v${result.entry.缓存版本}，${result.entry.历史领料单数} 条历史领用关系保持不变`)
  await Promise.all([loadMeta(), loadAll()])
}

function conclusionTag(conclusion: string) {
  if (!conclusion) return 'readonly'
  if (conclusion.includes('允许')) return 'ok'
  if (conclusion.includes('拒绝')) return 'blocked'
  if (conclusion.includes('只读')) return 'readonly'
  return 'readonly'
}
function exceptionTag(status: string) {
  if (status === '已批准') return 'ok'
  if (status === '已驳回') return 'blocked'
  return 'readonly'
}
function requisitionTag(status: string) {
  if (status === '已出库') return 'ok'
  if (status === '已拒绝') return 'blocked'
  if (status === '已取消') return 'readonly'
  return 'pending'
}

onMounted(async () => {
  await loadMeta()
  await loadAll()
})
</script>

<style scoped>
.tabs { display: flex; gap: 6px; margin: 10px 0 14px; border-bottom: 1px solid var(--border); }
.tab { border: none; background: none; padding: 8px 14px; cursor: pointer; font-size: 13px; color: var(--muted); border-bottom: 2px solid transparent; }
.tab.active { color: var(--brand); border-bottom-color: var(--brand); font-weight: 600; }
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
.panel h3 { margin: 0 0 6px; font-size: 15px; }
.panel h4 { margin: 18px 0 8px; font-size: 13px; }
.hint { color: var(--muted); font-size: 12px; margin: 4px 0 10px; }
.notice { padding: 8px 12px; border-radius: 6px; font-size: 13px; background: #ecfdf3; color: #027a48; border: 1px solid #abefc6; }
.notice.error { background: #fef3f2; color: #b42318; border-color: #fda29b; }
.form-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px 14px; margin: 12px 0 16px; }
.form-grid .wide { grid-column: span 2; }
.form-actions { display: flex; align-items: flex-end; }
.filter-item select, .filter-item input { width: 100%; padding: 5px 8px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.decision { display: flex; justify-content: space-between; gap: 16px; align-items: center; padding: 12px 14px; border-radius: 8px; border: 1px solid; margin: 10px 0; }
.decision.allow { background: #ecfdf3; border-color: #abefc6; }
.decision.deny { background: #fef3f2; border-color: #fda29b; }
.decision.readonly-decision { background: #fffaeb; border-color: #fedf89; }
.decision strong { display: block; font-size: 15px; margin-bottom: 4px; }
.decision-source { font-size: 12px; color: var(--muted); margin-right: 10px; }
.decision-note { font-size: 12px; color: var(--muted); }
.quota { display: flex; gap: 14px; font-size: 12px; color: var(--muted); white-space: nowrap; }
.quota b { display: block; font-size: 16px; color: #1f2937; text-align: center; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; }
.tag.ok { background: #ecfdf3; color: #027a48; }
.tag.blocked { background: #fef3f2; color: #b42318; }
.tag.readonly { background: #f2f4f7; color: #475467; }
.tag.pending { background: #fffaeb; color: #b54708; }
.link.danger { color: #b42318; }
.rejected { background: #fff7f7; }
.muted { color: var(--muted); }
.reason-cell, .conclusion-cell { max-width: 240px; font-size: 12px; color: #475467; }
</style>
