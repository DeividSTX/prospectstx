"""Motor determinístico. Sinais sem evidência nunca produzem observações."""
from typing import Protocol


class AnalysisProvider(Protocol):
    def analyze(self, lead: dict, config: dict) -> dict: ...


class RulesAnalyzer:
    def analyze(self, lead, config):
        evidence = lead.get('evidence', {})
        niche = str(lead.get('niche', '')).casefold()
        is_marketing_agency = niche == 'agencia_marketing'
        verified_notes = ' '.join(str(v.get('note', '')) for v in evidence.values() if isinstance(v, dict) and v.get('verified') is True)
        agency_context = (str(lead.get('description', '')) + ' ' + verified_notes).casefold()
        commercial_intent = is_marketing_agency and any(term in agency_context for term in ('diagnóstico','diagnostico','orçamento','orcamento','solicite','fale conosco','agende','contato'))
        performance_signal = is_marketing_agency and any(term in agency_context for term in ('tráfego','trafego','performance','mídia paga','midia paga','google ads','meta ads','gestão de tráfego','gestao de trafego'))
        automation_signal = is_marketing_agency and any(term in agency_context for term in ('automação','automacao','inteligência artificial','inteligencia artificial',' ia ','chatbot','crm','whatsapp','atendimento automático','atendimento automatico'))
        opportunities = []
        for icp in config['icps']:
            item = evidence.get(icp['signal'], {})
            if isinstance(item, dict) and item.get('verified') is True and item.get('source') and item.get('note'):
                opportunities.append({'service': icp['service'], 'observation': icp['observation'],
                                      'hypothesis': icp['hypothesis'], 'evidence': item})
        target = config.get('target_niches', [])
        excluded = lead.get('niche', '').casefold() in [n.casefold() for n in config.get('excluded_niches', [])]
        fit = bool(lead.get('niche')) and (not target or lead['niche'].casefold() in [n.casefold() for n in target])
        criteria = {'icp': fit and not excluded, 'opportunity': bool(opportunities),
                    'contact': bool(lead.get('email') or lead.get('phone') or lead.get('instagram')),
                    'offer': bool(lead.get('offer')),
                    'activity': bool(evidence.get('recent_activity', {}).get('verified') is True and evidence.get('recent_activity', {}).get('source') and evidence.get('recent_activity', {}).get('note'))}
        if is_marketing_agency:
            criteria = {'agency_fit': True, 'commercial_intent': commercial_intent, 'performance': performance_signal, 'automation': automation_signal, 'contact': bool(lead.get('email') or lead.get('phone') or lead.get('instagram'))}
            weights = {'agency_fit': 25, 'commercial_intent': 20, 'performance': 20, 'automation': 25, 'contact': 10}
        else:
            weights = config['weights']
        breakdown = [{'criterion': k, 'points': weights[k] if v else 0, 'maximum': weights[k],
                      'reason': 'Critério registrado na base.' if v else 'Não verificado ou não atendido.'} for k, v in criteria.items()]
        score = round(sum(c['points'] for c in breakdown) * 100 / sum(weights.values()))

        qualified = (
            (score >= config['min_lead_score'] and not excluded)
            if is_marketing_agency
            else (bool(opportunities) and score >= config['min_lead_score'] and not excluded)
        )

        return {
            'engine': 'rules',
            'score': score,
            'breakdown': breakdown,
            'opportunities': opportunities,
            'qualified': qualified,
            'classification': (
                'EXCLUDED' if excluded
                else 'HIGH_PRIORITY' if qualified and score >= 61
                else 'OPPORTUNITY' if qualified
                else 'NO_CLEAR_OPPORTUNITY'
            ),
            'priority': (
                'máxima' if score > 80
                else 'alta' if score > 60
                else 'média' if score > 30
                else 'baixa'
            ),
            'answers': {
                'O que vende?': lead.get('offer') or 'Não verificado',
                'Para quem vende?': lead.get('audience') or 'Não verificado',
                'Como consegue clientes?': lead.get('acquisition') or 'Não verificado',
                'Principal oferta': lead.get('offer') or 'Não verificado',
                'Serviço relevante': opportunities[0]['service'] if opportunities else 'Não verificado'
            },
            'explanation': (
                'Perfil de agência avaliado pelos sinais comerciais e operacionais presentes nas evidências registradas. A ausência de um sinal não comprova ausência de necessidade.'
                if is_marketing_agency
                else (
                    'Oportunidades são hipóteses baseadas nas evidências fornecidas.'
                    if opportunities
                    else 'Nenhum sinal com evidência verificável foi registrado. Ausência de dados não comprova ausência de site ou necessidade.'
                )
            )
        }


def generate_messages(lead, result, agency):
    if not result['qualified']:
        return []
    opportunity = result['opportunities'][0] if result['opportunities'] else {'evidence': lead.get('evidence', {}).get('prospect', {}), 'hypothesis': 'Posso compartilhar uma sugestão prática para otimizar essa operação.'}
    context = f"Oi, equipe da {lead['company']}."
    observation = opportunity['evidence']['note']
    hypothesis = opportunity['hypothesis']
    prospect_observation = observation
    if str(lead.get('niche', '')).casefold() == 'agencia_marketing':
        prospect_observation = 'vocês trabalham com performance e já direcionam o público para um diagnóstico.'
    return [('A', f'{context} Notei que {prospect_observation} Tive uma ideia que pode ajudar a aproveitar melhor essa etapa da captação. Posso te mostrar rapidamente? - {agency}'),
            ('B', f'{context} Estava analisando o trabalho de vocês e percebi que {prospect_observation} Enxerguei uma possibilidade de otimizar essa jornada entre o conteúdo e a entrada de novos contatos. Posso compartilhar o que pensei? - {agency}'),
            ('C', f'{context} Percebi que {prospect_observation} Vi uma oportunidade nessa etapa que talvez esteja passando despercebida. Quer que eu te mostre em 2 minutos o que identifiquei? - {agency}')]