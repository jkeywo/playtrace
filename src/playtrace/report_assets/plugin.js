/* Generic plugin replay. No game rules are interpreted by this renderer. */
var PlayTraceReplay = (() => {
  const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
  const pretty = v => String(v).replace(/[_.:]/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  const equal = (a, b) => {
    if (a === b) return true;
    if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => equal(v, b[i]));
    if (object(a) && object(b)) return Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(k => Object.hasOwn(b, k) && equal(a[k], b[k]));
    return false;
  };
  function names(data) {
    const map = new Map();
    for (const [id, card] of Object.entries(data.card_names || {})) {
      map.set(id, card.name || id);
      if (card.id) map.set(card.id, card.name || id);
    }
    for (const group of ['players', 'locations']) {
      for (const [id, value] of Object.entries(data[group] || {})) map.set(id, value?.name || pretty(id));
    }
    return map;
  }
  function display(value, lookup) {
    if (value === undefined) return 'Not present';
    if (value === null) return 'None';
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    if (typeof value === 'string') return lookup.get(value) || value;
    if (Array.isArray(value)) return value.length ? value.map(v => display(v, lookup)).join(', ') : 'None';
    if (object(value) && value.name) return `${value.name}${value.track && value.value !== undefined ? ` (${value.track}: ${value.value})` : ''}`;
    if (object(value)) return Object.entries(value).map(([k, v]) => `${lookup.get(k) || pretty(k)}: ${display(v, lookup)}`).join('; ') || 'None';
    return String(value);
  }
  const labels = {at:'Location', position:'Cards drawn', cards:'Cards in play', hand:'Cards in hand',
    capabilities:'Available capabilities', local_icons:'Local icons', recipes:'Available recipes',
    discard:'Discard pile', set_aside:'Set aside', deck:'Ordered deck', built:'Crafted cards',
    players_present:'Players present', value:'Track value'};
  const label = (key, lookup) => lookup.get(key) || labels[key] || pretty(key);
  function diff(before, after, path = []) {
    if (equal(before, after)) return [];
    if (object(before) && object(after)) return [...new Set([...Object.keys(before), ...Object.keys(after)])]
      .flatMap(key => diff(before[key], after[key], [...path, key]));
    return [{path, before, after}];
  }
  function tabs(state) {
    if (!state) return [{id:'overview', name:'Overview', value:{checkpoint:'Unavailable'}}];
    const data = state.kind === 'plugin' ? state.data || {} : state;
    const overview = Object.fromEntries(Object.entries(data).filter(([k]) => !['players','locations','card_names'].includes(k)));
    const result = [{id:'overview', name:'Overview', value:{turn:state.turn, status:state.status, ...overview}}];
    for (const group of ['players','locations']) {
      if (!object(data[group])) continue;
      for (const [id, v] of Object.entries(data[group])) {
        const value = object(v) ? {...v} : {state:v};
        if (group === 'locations' && object(data.players)) value.players_present = Object.keys(data.players).filter(p => data.players[p]?.at === id);
        result.push({id:`${group}:${id}`, name:value.name || pretty(id), kind:group, value});
      }
    }
    return result;
  }
  function changesFor(before, after) {
    const old = new Map(tabs(before).map(t => [t.id, t]));
    const current = tabs(after);
    // Removed players/locations remain inspectable at the action which removes them.
    for (const t of old.values()) if (!current.some(n => n.id === t.id)) current.push({...t, value:undefined});
    return current.map(t => ({...t, changes:before && after ? diff(old.get(t.id)?.value, t.value) : []}));
  }
  function description(event) {
    return event?.data?.description || (event ? `${pretty(event.event_type)}.` : 'No actions recorded.');
  }
  function involvedCards(event, before, after) {
    const data = s => s?.kind === 'plugin' ? s.data || {} : s || {};
    const old = data(before), current = data(after);
    const definitions = {...old.card_names, ...current.card_names};
    const ids = new Set(event?.entity_ids || []);
    const add = key => {if(definitions[key]?.id) ids.add(definitions[key].id);};
    for (const group of ['players','locations']) {
      for (const key of new Set([...Object.keys(old[group] || {}), ...Object.keys(current[group] || {})])) {
        const a = old[group]?.[key] || {}, b = current[group]?.[key] || {};
        for (const card of new Set([...Object.keys(a.cards || {}), ...Object.keys(b.cards || {})])) {
          if(!equal(a.cards?.[card],b.cards?.[card])) add(card);
        }
        for (const pile of ['hand','discard','set_aside','recipes']) {
          const aa = a[pile] || [], bb = b[pile] || [];
          for(const card of new Set([...aa,...bb])) if(aa.filter(c=>c===card).length !== bb.filter(c=>c===card).length) add(card);
        }
      }
    }
    return [...ids];
  }
  const safeImage = url => typeof url === 'string' && /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(url);
  function changeText(change, lookup) {
    if (Array.isArray(change.before) && Array.isArray(change.after)) {
      const remainder = [...change.before];
      const added = change.after.filter(item => {
        const i = remainder.findIndex(old => equal(old, item));
        if (i < 0) return true;
        remainder.splice(i, 1); return false;
      });
      return [added.length ? `Added ${display(added, lookup)}` : '', remainder.length ? `Removed ${display(remainder, lookup)}` : ''].filter(Boolean).join(' · ') || 'Order changed';
    }
    const delta = typeof change.before === 'number' && typeof change.after === 'number' ? change.after - change.before : null;
    return `${display(change.before, lookup)} → ${display(change.after, lookup)}${delta !== null ? ` (${delta > 0 ? '+' : ''}${delta})` : ''}`;
  }
  function mount(root, traces) {
    const q = id => root.querySelector('#pt-' + id);
    const el = (tag, text, className) => {const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (className) n.className = className; return n;};
    let ti = 0, ei = 0, selected = 'overview', logTrace = -1, logPosition = -1;
    const button = (text, action, cls) => {const b = el('button', text, cls); b.type = 'button'; b.onclick = action; return b;};
    function faces(parent, card, select, selectedFace) {
      parent.replaceChildren(); parent.className = 'card-face-controls';
      for (const face of ['front','back']) if(safeImage(card[face])) {
        const b=button(pretty(face),()=>{select(face); [...parent.children].forEach(n=>n.setAttribute('aria-pressed',String(n===b)));});
        b.setAttribute('aria-pressed',String(face === (selectedFace || (safeImage(card.front) ? 'front':'back')))); parent.append(b);
      }
    }
    function showCard(card, selectedFace) {
      q('card-title').textContent=card.name;
      q('card-note').textContent=card.note || '';
      const select=face=>{q('card-large').src=card[face];q('card-large').alt=`${card.name} — ${face}`;
        q('card-download').href=card[face];q('card-download').download=`${card.name.replace(/[^a-z0-9-]/gi,'_')}-${face}.${card[face].startsWith('data:image/png')?'png':card[face].startsWith('data:image/webp')?'webp':'jpg'}`;};
      faces(q('card-faces'),card,select,selectedFace);select(selectedFace || (safeImage(card.front)?'front':'back')); q('card-dialog').showModal();
    }
    q('card-close').onclick=()=>q('card-dialog').close();
    function renderCards(t,event,previous,state,lookup) {
      const ids = involvedCards(event,previous,state);
      q('cards').replaceChildren(); q('card-panel').hidden=!ids.length || !t.card_images;
      if(!t.card_images) return;
      for(const id of ids) {
        const card=t.card_images[id], figure=el('figure');
        if(!card || !['front','back'].some(face=>safeImage(card[face]))) {
          figure.append(el('figcaption',lookup.get(id)||id),el('p','Image unavailable','muted'));q('cards').append(figure);continue;
        }
        const img=el('img'); img.loading='lazy';
        let selectedFace=safeImage(card.front)?'front':'back';
        const picture=button('',()=>showCard(card,selectedFace),'card-picture');picture.setAttribute('aria-label',`Enlarge ${card.name}`);picture.append(img);
        const controls=el('div'); const select=face=>{selectedFace=face;img.src=card[face];img.alt=`${card.name} — ${face}`;};
        faces(controls,card,select);select(safeImage(card.front)?'front':'back');
        figure.append(picture,el('figcaption',card.name),controls);q('cards').append(figure);
      }
    }
    traces.forEach((t, i) => {const o = el('option', t.label); o.value = i; q('trace').append(o);});
    function selectTab(id) {selected = id; render(); q('tabs').querySelector('[aria-selected=true]')?.focus();}
    function renderFields(parent, value, previous, lookup, path = []) {
      if (!object(value)) {parent.append(el('p', display(value, lookup))); return;}
      const piles = {};
      if (Array.isArray(value.deck)) {
        value = {...value};
        for (const k of ['deck','set_aside','discard','recipes']) if (k in value) {piles[k] = value[k]; delete value[k];}
      }
      const fields = el('div', undefined, 'fields'); parent.append(fields);
      for (const [key, val] of Object.entries(value)) {
        if (key === 'id' || key === 'name') continue;
        const old = previous?.[key], changed = previous !== undefined && !equal(val, old);
        if (object(val) && Object.keys(val).length) continue;
        const field = el('div', undefined, 'field' + (changed ? ' changed' : ''));
        field.append(el('span', key === 'value' && value.track ? value.track : label(key, lookup), 'field-label'));
        if (Array.isArray(val) && val.length) {
          const chips = el('div', undefined, 'tokens');
          val.forEach((item, i) => chips.append(el('span', (key === 'deck' ? `${i + 1}. ` : '') + display(item, lookup), 'token')));
          field.append(chips);
        } else field.append(el('span', changed ? changeText({before:old,after:val}, lookup) : display(val, lookup), 'field-value'));
        fields.append(field);
      }
      const priority = ['resources','capabilities','local_icons','cards'];
      const entries = Object.entries(value).sort(([a],[b]) => (priority.includes(a) ? priority.indexOf(a) : 99) - (priority.includes(b) ? priority.indexOf(b) : 99));
      for (const [key, val] of entries) {
        if (!object(val) || !Object.keys(val).length) continue;
        const group = el('section', undefined, 'group'); group.append(el('h3', val.name || label(key, lookup)));
        renderFields(group, val, previous?.[key], lookup, [...path,key]); parent.append(group);
      }
      if (Object.keys(piles).length) {
        const section = el('details', undefined, 'group'); section.append(el('summary','Ordered deck, recipes and card piles'));
        // Avoid treating this pile-only object as another location.
        const deck = piles.deck; delete piles.deck;
        renderFields(section, piles, previous, lookup, path);
        const ordered = el('ol'); deck.forEach((key,i)=>{const item=el('li',display(key,lookup));
          if(i === value.position) item.append(el('strong',' ← Next card'));
          if(i < value.position) item.append(el('span',' · drawn','muted')); ordered.append(item);});
        section.append(el('h3','Ordered deck'), ordered); parent.append(section);
      }
    }
    function render() {
      const t = traces[ti]; if (!t) return;
      const event = t.events[ei], state = t.states[ei], previous = ei ? t.states[ei-1] : t.initial_state;
      const data = state?.kind === 'plugin' ? state.data : state || {};
      const lookup = names({...((previous?.kind === 'plugin' ? previous.data : previous) || {}), ...data});
      const all = changesFor(previous, state);
      if (!all.some(tab => tab.id === selected)) selected = 'overview';
      q('summary').textContent = `${t.plugin_id} · ${pretty(t.status)} · ${t.metrics.turns_completed} turns · Seed ${t.seed}`;
      q('position').max = Math.max(0,t.events.length-1); q('position').value = ei;
      q('prev').disabled = ei === 0; q('next').disabled = ei >= t.events.length-1;
      q('counter').textContent = t.events.length ? `Action ${ei+1} of ${t.events.length}` : 'No actions';
      q('phase').textContent = `Turn ${event?.turn ?? state?.turn ?? 0} / ${data.phase || 'Replay'}`;
      q('description').textContent = description(event);
      renderCards(t,event,previous,state,lookup);
      q('affected').replaceChildren(el('span', state ? 'Changed here:' : 'State checkpoint unavailable.'));
      const affected = all.filter(tab => tab.changes.length);
      if (!affected.length && state) q('affected').append(el('span', ei === 0 ? 'Initial state.' : previous ? 'No state changes in this action.' : 'Previous checkpoint unavailable; changes cannot be compared.', 'muted'));
      affected.forEach(tab => q('affected').append(button(`${tab.name} · ${tab.changes.length}`, () => selectTab(tab.id), 'change-chip')));
      q('tabs').replaceChildren();
      all.forEach((tab, i) => {
        const b = button(tab.name, () => selectTab(tab.id)); b.id = `pt-tab-${i}`; b.setAttribute('role','tab');
        b.setAttribute('aria-selected', String(selected === tab.id)); b.setAttribute('aria-controls','pt-tab-content'); b.tabIndex = selected === tab.id ? 0 : -1;
        b.title = `${tab.kind ? pretty(tab.kind) + ': ' : ''}${tab.name}${tab.changes.length ? ` — ${tab.changes.length} changes in this action` : ''}`;
        if (tab.changes.length) b.append(el('span', `${tab.changes.length} changed`, 'badge'));
        b.onkeydown = e => {let n; if (e.key === 'ArrowRight') n=(i+1)%all.length; if(e.key === 'ArrowLeft') n=(i+all.length-1)%all.length; if(e.key === 'Home') n=0; if(e.key === 'End') n=all.length-1; if(n !== undefined){e.preventDefault();selectTab(all[n].id);}};
        q('tabs').append(b);
      });
      const active = all.find(tab => tab.id === selected), panel = q('tab-content');
      panel.setAttribute('aria-labelledby', `pt-tab-${all.indexOf(active)}`); panel.replaceChildren(el('h2', active.name));
      panel.append(el('p', 'State after this action. Gold highlights show changes since the previous action.', 'muted'));
      if (active.changes.length) {
        const list = el('ul', undefined, 'change-list');
        active.changes.forEach(change => {const li=el('li'); li.append(el('strong', (change.path.map(k=>label(k,lookup)).join(' › ') || active.name)+': '), document.createTextNode(changeText(change,lookup))); list.append(li);});
        panel.append(list);
      }
      const old = previous ? tabs(previous).find(tab=>tab.id === selected)?.value : undefined;
      renderFields(panel, active.value, old, lookup);
      if (logTrace !== ti) {
        q('log').replaceChildren(); logTrace = ti; logPosition = -1;
        t.events.forEach((ev, i) => {const li=el('li'); const b=button('',()=>{ei=i;render();}); b.append(el('small',`${i+1} · Turn ${ev.turn}`),document.createTextNode(description(ev))); li.append(b);q('log').append(li);});
      }
      [...q('log').querySelectorAll('button')].forEach((b,i)=>b.setAttribute('aria-current',String(i===ei)));
      if (logPosition !== ei) {
        const current = q('log').querySelector('[aria-current=true]');
        if (current) {
          const item = current.getBoundingClientRect(), box = q('log').getBoundingClientRect();
          if (item.top < box.top) q('log').scrollTop += item.top - box.top;
          else if (item.bottom > box.bottom) q('log').scrollTop += item.bottom - box.bottom;
        }
        logPosition = ei;
      }
      q('event').textContent = JSON.stringify(event || {},null,2);
      q('state').textContent = JSON.stringify(state || {},null,2);
      q('blockers').textContent = t.blockers.length ? JSON.stringify(t.blockers,null,2) : 'No blockers.';
    }
    q('trace').onchange=e=>{ti=Number(e.target.value);ei=0;selected='overview';render();};
    q('position').oninput=e=>{ei=Number(e.target.value);render();};
    q('prev').onclick=()=>{ei=Math.max(0,ei-1);render();};
    q('next').onclick=()=>{ei=Math.min(Math.max(0,traces[ti].events.length-1),ei+1);render();};
    render();
  }
  return {mount, equal, diff, tabs, changesFor, names, display, description, changeText, involvedCards, safeImage};
})();
if (typeof module !== 'undefined') module.exports = PlayTraceReplay;
