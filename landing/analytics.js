/* FLEET ANALYTICS — drop-in, no build step, no npm dependency.
 *
 * HOW TO USE
 *   1. Copy this file to the project:
 *        - Next.js / Vite / any framework with a static dir  ->  public/analytics.js
 *        - Plain static site                                 ->  next to index.html
 *   2. Change PROJECT_SLUG below to the project's folder name (kebab-case).
 *   3. Reference it once from the page <head>:
 *        - Static HTML : <script src="analytics.js" defer></script>
 *        - Next.js app router, in app/layout.tsx <head>:
 *              <script src="/analytics.js" defer />
 *        - Vite, in index.html <head>:
 *              <script src="/analytics.js" defer></script>
 *   4. Done. Visitors show up on the Fleet Overview dashboard within ~60s.
 *
 * WHY A FILE AND NOT THE SDK
 *   Every site in the fleet needs the same thing: anonymous pageviews, tagged with
 *   which site they came from. Using `posthog-js` as an npm dependency would mean a
 *   package.json change, a lockfile change, and a provider component in ~10 repos on
 *   three different frameworks. This is one file and one script tag, identical
 *   everywhere, and it cannot break a build.
 *
 *   Projects that ALREADY use the posthog-js SDK (because they track custom product
 *   events) should NOT use this file. They keep their SDK and just add the register
 *   call — see README.md, "Projects that already have the SDK".
 *
 * DEPTH
 *   Pageviews only. autocapture off, no identify(), no session replay. That keeps the
 *   data anonymous (no consent banner needed) and keeps the shared project's event
 *   taxonomy clean. Products that need custom events graduate to the typed registry
 *   in analytics.ts — see README.md.
 */
(function () {
  'use strict';

  // ─── CHANGE THIS ONE LINE PER PROJECT ──────────────────────────────────────
  var PROJECT_SLUG = 'founder-crm-landing';
  // ───────────────────────────────────────────────────────────────────────────

  var POSTHOG_KEY = 'phc_oVPCPUJcdtiYuxKVYjnUUZAAfvxgaRfQTbJtifoqZspr';
  var POSTHOG_HOST = 'https://us.i.posthog.com';

  if (PROJECT_SLUG === 'REPLACE_ME') {
    console.warn('[analytics] PROJECT_SLUG not set — not reporting.');
    return;
  }

  // Never report from local dev or a file:// open. Without this guard the shared
  // project fills up with a `localhost:3000` bucket that belongs to no site.
  var h = location.hostname;
  if (location.protocol === 'file:' || h === 'localhost' || h === '127.0.0.1' || h === '' || h === '[::1]') return;

  // Official PostHog HTML snippet loader (posthog.com/docs).
  !function(t,e){var o,n,p,r;e.__SV||(window.posthog && window.posthog.__loaded)||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=s.api_host.replace(".i.posthog.com","-assets.i.posthog.com")+"/static/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="init capture register register_once register_once register_for_session unregister unregister_for_session getFeatureFlag getFeatureFlagResult isFeatureEnabled reloadFeatureFlags updateEarlyAccessFeatureEnrollment getEarlyAccessFeatures on onFeatureFlags onSessionId getSurveys getActiveMatchingSurveys renderSurvey canRenderSurvey getNextSurveyStep identify setPersonProperties group resetGroups setPersonPropertiesForFlags resetPersonPropertiesForFlags setGroupPropertiesForFlags resetGroupPropertiesForFlags reset get_distinct_id getGroups get_session_id get_session_replay_url alias set_config startSessionRecording stopSessionRecording sessionRecordingStarted captureException loadToolbar get_property getSessionProperty createPersonProfile opt_in_capturing opt_out_capturing has_opted_in_capturing has_opted_out_capturing clear_opt_in_out_capturing debug".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);

  posthog.init(POSTHOG_KEY, {
    api_host: POSTHOG_HOST,
    defaults: '2026-05-30',        // captures history-change pageviews (SPA routing)
    person_profiles: 'identified_only',
    autocapture: false             // pageviews only
  });

  // THE FLEET KEY. Every event carries which site it came from. $host is not a
  // substitute — preview URLs, custom domains and IP hosts all fragment it.
  posthog.register({ project: PROJECT_SLUG });
})();
