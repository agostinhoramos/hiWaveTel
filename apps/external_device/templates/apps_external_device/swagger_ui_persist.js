"use strict";

const swaggerSettings = {{ settings|safe }};
const schemaAuthNames = {{ schema_auth_names|safe }};
let schemaAuthFailed = false;
const plugins = [];
const AUTH_STORAGE_KEY = "hiwavetel.swagger.authorized";

const loadStoredAuth = () => {
  const raw = localStorage.getItem(AUTH_STORAGE_KEY) || localStorage.getItem("authorized");
  if (!raw) {
    return undefined;
  }
  try {
    return JSON.parse(raw);
  } catch {
    return undefined;
  }
};

const persistAuthorizedState = () => {
  if (!uiInitialized()) {
    return;
  }
  try {
    const state = ui.getState().get("auth").get("authorized");
    if (state === undefined) {
      return;
    }
    const auth = state.toJS();
    if (Object.keys(auth).length === 0) {
      localStorage.removeItem(AUTH_STORAGE_KEY);
      localStorage.removeItem("authorized");
      return;
    }
    const serialized = JSON.stringify(auth);
    localStorage.setItem(AUTH_STORAGE_KEY, serialized);
    // Keep Swagger's conventional key too.
    localStorage.setItem("authorized", serialized);
  } catch (e) {
    console.warn("could not persist swagger authorization", e);
  }
};

const restoreAuthorizedState = () => {
  const authorized = loadStoredAuth();
  if (!authorized || !uiInitialized()) {
    return;
  }
  try {
    ui.authActions.authorize(authorized);
  } catch (e) {
    console.warn("could not restore swagger authorization", e);
  }
};

const clearStoredAuth = () => {
  localStorage.removeItem(AUTH_STORAGE_KEY);
  localStorage.removeItem("authorized");
};

const persistAuthOnChange = () => {
  return {
    statePlugins: {
      auth: {
        wrapActions: {
          authorize: (ori) => (...args) => {
            const res = ori(...args);
            setTimeout(persistAuthorizedState, 0);
            return res;
          },
          authorizeOauth2: (ori) => (...args) => {
            const res = ori(...args);
            setTimeout(persistAuthorizedState, 0);
            return res;
          },
          logout: (ori) => (...args) => {
            const res = ori(...args);
            clearStoredAuth();
            return res;
          },
        },
      },
    },
  };
};

plugins.push(persistAuthOnChange);

const reloadSchemaOnAuthChange = () => {
 return {
 statePlugins: {
 auth: {
 wrapActions: {
 authorizeOauth2:(ori) => (...args) => {
 schemaAuthFailed = false;
 setTimeout(() => ui.specActions.download());
 return ori(...args);
 },
 authorize: (ori) => (...args) => {
 schemaAuthFailed = false;
 setTimeout(() => ui.specActions.download());
 return ori(...args);
 },
 logout: (ori) => (...args) => {
 schemaAuthFailed = false;
 setTimeout(() => ui.specActions.download());
 return ori(...args);
 },
 },
 },
 },
 };
};

if (schemaAuthNames.length > 0) {
 plugins.push(reloadSchemaOnAuthChange);
}

const uiInitialized = () => {
 try {
 ui;
 return true;
 } catch {
 return false;
 }
};

const isSchemaUrl = (url) => {
 if (!uiInitialized()) {
 return false;
 }
 return url === new URL(ui.getConfigs().url, document.baseURI).href;
};

const responseInterceptor = (response, ...args) => {
 if (!response.ok && isSchemaUrl(response.url)) {
 console.warn("schema request received '" + response.status + "'. disabling credentials for schema till logout.");
 if (!schemaAuthFailed) {
 // only retry once to prevent endless loop.
 schemaAuthFailed = true;
 setTimeout(() => ui.specActions.download());
 }
 }
 return response;
};

const injectAuthCredentials = (request) => {
 let authorized;
 if (uiInitialized()) {
 const state = ui.getState().get("auth").get("authorized");
 if (state !== undefined && Object.keys(state.toJS()).length !== 0) {
 authorized = state.toJS();
 }
 } else {
 authorized = loadStoredAuth();
 }
 if (authorized === undefined) {
 return;
 }
 for (const authName of schemaAuthNames) {
 const authDef = authorized[authName];
 if (authDef === undefined || authDef.schema === undefined) {
 continue;
 }
 if (authDef.schema.type === "http" && authDef.schema.scheme === "bearer") {
 request.headers["Authorization"] = "Bearer " + authDef.value;
 return;
 } else if (authDef.schema.type === "http" && authDef.schema.scheme === "basic") {
 request.headers["Authorization"] = "Basic " + btoa(authDef.value.username + ":" + authDef.value.password);
 return;
 } else if (authDef.schema.type === "apiKey" && authDef.schema.in === "header") {
 request.headers[authDef.schema.name] = authDef.value;
 return;
 } else if (authDef.schema.type === "oauth2" && authDef.token.token_type === "Bearer") {
 request.headers["Authorization"] = `Bearer ${authDef.token.access_token}`;
 return;
 }
 }
};

const requestInterceptor = (request, ...args) => {
 if (request.loadSpec && schemaAuthNames.length > 0 && !schemaAuthFailed) {
 try {
 injectAuthCredentials(request);
 } catch (e) {
 console.error("schema auth injection failed with error: ", e);
 }
 }
 // selectively omit adding headers to mitigate CORS issues.
 if (!["GET", undefined].includes(request.method) && request.credentials === "same-origin") {
 request.headers["{{ csrf_header_name }}"] = "{{ csrf_token }}";
 }
 return request;
};

const ui = SwaggerUIBundle({
 url: "{{ schema_url|escapejs }}",
 dom_id: "#swagger-ui",
 presets: [SwaggerUIBundle.presets.apis],
 plugins,
 layout: "BaseLayout",
 requestInterceptor,
 responseInterceptor,
 // Keep authorize values after refresh in default Swagger behavior.
 persistAuthorization: true,
 ...swaggerSettings,
});

setTimeout(restoreAuthorizedState, 100);

{% if oauth2_config %}ui.initOAuth({{ oauth2_config|safe }});{% endif %}
