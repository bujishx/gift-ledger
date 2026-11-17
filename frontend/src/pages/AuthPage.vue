<script setup>
import { computed, ref, toRefs, watch } from 'vue';
import './auth-page.css';

/* 任务 C（U12）：登录 / 注册 / 首次本人建档三屏的视觉统一。
 * state 字段（account / authMode / credentials / profileForm / busy / error / notice）
 * 与 actions 名称（authSubmit / saveProfile）保持原样，认证机制、注册权限、
 * 账号隔离保持原样；确认密码与显示/隐藏状态只用于当前表单。 */
const props = defineProps({ state: { type: Object, required: true }, actions: { type: Object, required: true } });
const { account, authMode, credentials, profileForm, busy, error, notice } = toRefs(props.state);
const { authSubmit, saveProfile } = props.actions;

/* 页面内部辅助状态：提交期间禁止重复点击，不改动传入的业务状态。 */
const submitting = ref(false);

const isRegister = computed(() => authMode.value === 'register');
const confirmation = ref('');
const confirmationChecked = ref(false);
const confirmationInput = ref(null);
const showPassword = ref(false);
const showConfirmation = ref(false);
const confirmationError = computed(() => isRegister.value && confirmationChecked.value && credentials.value.password !== confirmation.value);
watch(isRegister, () => {
  confirmation.value = '';
  confirmationChecked.value = false;
  showPassword.value = false;
  showConfirmation.value = false;
});
/* 注册用新密码，登录用当前密码，方便浏览器/密码管理器正确处理。 */
const passwordAutocomplete = computed(() => (isRegister.value ? 'new-password' : 'current-password'));
/* 业务层的 busy 与页面内的提交中标记合并，用于禁用按钮和显示进行中文案。 */
const pending = computed(() => Boolean(busy.value) || submitting.value);
const authButtonLabel = computed(() => {
  if (pending.value) return isRegister.value ? '注册中…' : '登录中…';
  return isRegister.value ? '注册' : '登录';
});
const profileButtonLabel = computed(() => (pending.value ? '保存中…' : '开始使用'));

async function run(action) {
  if (pending.value) return;
  submitting.value = true;
  try {
    await action();
  } finally {
    submitting.value = false;
  }
}

async function submitAuth() {
  if (isRegister.value) {
    confirmationChecked.value = true;
    if (confirmationError.value) {
      confirmationInput.value?.focus();
      return;
    }
  }
  await run(authSubmit);
}

function switchMode() {
  authMode.value = isRegister.value ? 'login' : 'register';
  error.value = '';
  notice.value = '';
}
</script>

<template>
  <main v-if="!account" class="auth-shell auth-page" data-auth-screen="login">
    <section class="auth-card card" aria-labelledby="auth-title">
      <header class="auth-head">
        <div class="brand large">
          <img class="brand-mark brand-mark-image" src="/brand-mark.png" alt="" /><strong>往来有礼</strong>
        </div>
        <h1 id="auth-title">{{ isRegister ? "创建账号" : "欢迎回来" }}</h1>
        <p class="muted">
          {{
            isRegister
              ? "设置登录名和密码，注册并登录后再建立你的本人档案。"
              : "记人情往来，守礼数分寸"
          }}
        </p>
      </header>
      <form class="form-stack" @submit.prevent="submitAuth">
        <div class="field"
          ><label class="field-label" for="auth-username"
            >{{ isRegister ? '用户名' : '登录名' }}<span class="auth-req" aria-hidden="true">*</span></label
          ><input
            id="auth-username"
            v-model="credentials.username"
            :disabled="pending"
            name="username"
            type="text"
            required
            maxlength="30"
            :minlength="isRegister ? 4 : undefined"
            :pattern="isRegister ? '[A-Za-z][A-Za-z0-9_]{3,29}' : undefined"
            autocomplete="username"
            autocapitalize="none"
            spellcheck="false"
            :aria-describedby="isRegister ? 'auth-username-rule auth-required-note' : 'auth-required-note'"
        /><small v-if="isRegister" id="auth-username-rule" class="auth-field-help">4—30位，英文字母开头，可使用字母、数字和下划线；大写自动转为小写。</small></div>
        <div class="field">
          <label class="field-label" for="auth-password">密码<span class="auth-req" aria-hidden="true">*</span></label>
          <div class="auth-password-control"><input
            id="auth-password"
            v-model="credentials.password"
            :disabled="pending"
            name="password"
            :type="showPassword ? 'text' : 'password'"
            required
            :minlength="isRegister ? 8 : undefined"
            maxlength="128"
            :autocomplete="passwordAutocomplete"
            :aria-describedby="isRegister ? 'auth-password-rule auth-required-note' : 'auth-required-note'"
          /><button type="button" :disabled="pending" :aria-label="showPassword ? '隐藏密码' : '显示密码'" :aria-pressed="showPassword" aria-controls="auth-password" @click="showPassword = !showPassword">{{ showPassword ? '隐藏' : '显示' }}</button></div>
          <small v-if="isRegister" id="auth-password-rule" class="auth-field-help">8—128个字符，可使用字母、数字、符号或中文；不能全为空格，也不能使用过于常见的密码。</small>
        </div>
        <div v-if="isRegister" class="field">
          <label class="field-label" for="auth-password-confirmation">确认密码<span class="auth-req" aria-hidden="true">*</span></label>
          <div class="auth-password-control"><input
            id="auth-password-confirmation"
            ref="confirmationInput"
            v-model="confirmation"
            :disabled="pending"
            name="password_confirmation"
            :type="showConfirmation ? 'text' : 'password'"
            required
            maxlength="128"
            autocomplete="new-password"
            :aria-invalid="confirmationError"
            :aria-describedby="confirmationError ? 'auth-confirmation-rule auth-confirmation-error' : 'auth-confirmation-rule'"
            @blur="confirmationChecked = true"
          /><button type="button" :disabled="pending" :aria-label="showConfirmation ? '隐藏确认密码' : '显示确认密码'" :aria-pressed="showConfirmation" aria-controls="auth-password-confirmation" @click="showConfirmation = !showConfirmation">{{ showConfirmation ? '隐藏' : '显示' }}</button></div>
          <small id="auth-confirmation-rule" class="auth-field-help">再次输入相同的密码。</small>
          <small v-if="confirmationError" id="auth-confirmation-error" class="auth-field-error" role="alert">两次输入的密码不一致</small>
        </div>
        <p id="auth-required-note" class="auth-note">
          <span class="auth-req" aria-hidden="true">*</span> 为必填项
        </p>
        <button
          class="primary full"
          type="submit"
          :disabled="pending"
          :aria-busy="pending"
        >
          {{ authButtonLabel }}
        </button>
      </form>
      <div class="auth-switch">
        <button class="text-button" type="button" :disabled="pending" @click="switchMode">
          {{ isRegister ? "已有账号？返回登录" : "没有账号？立即注册" }}
        </button>
      </div>
      <p v-if="error" class="alert error" role="alert">{{ error }}</p>
      <p v-else-if="notice" class="alert success" role="status">{{ notice }}</p>
    </section>
  </main>
  <main v-else-if="!account.profile_completed" class="auth-shell auth-page" data-auth-screen="onboarding">
    <section class="auth-card card" aria-labelledby="auth-profile-title">
      <header class="auth-head">
        <div class="brand large">
          <span class="brand-mark" aria-hidden="true">礼</span><strong>往来有礼</strong>
        </div>
        <h1 id="auth-profile-title">先完善本人信息</h1>
        <p class="muted">首次使用请填写真实姓名。账目收支将以“本人”为准自动判定。</p>
      </header>
      <p class="auth-hint">
        填写真实姓名后，系统会自动建立本人联系人，并据此区分收支：<br />
        你家办事（事主是你）→ 自动记为<b class="income">收入</b>；<br />
        别人办事你随礼 → 自动记为<b class="expense">支出</b>。
      </p>
      <form class="form-stack" @submit.prevent="run(saveProfile)">
        <label class="field"
          ><span class="field-label"
            >真实姓名<span class="auth-req" aria-hidden="true">*</span></span
          ><input
            v-model="profileForm.name"
            :disabled="pending"
            name="name"
            type="text"
            required
            maxlength="50"
            autocomplete="name"
            aria-describedby="auth-required-note"
        /></label>
        <p id="auth-required-note" class="auth-note">
          <span class="auth-req" aria-hidden="true">*</span> 为必填项
        </p>
        <button
          class="primary full"
          type="submit"
          :disabled="pending"
          :aria-busy="pending"
        >
          {{ profileButtonLabel }}
        </button>
      </form>
      <p v-if="error" class="alert error" role="alert">{{ error }}</p>
      <p v-else-if="notice" class="alert success" role="status">{{ notice }}</p>
    </section>
  </main>
</template>
