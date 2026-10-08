# ProspectSTX — publicação gratuita em piloto

Interface: https://prospecstxx.netlify.app

Projeto Supabase: https://jishtumlvwatbvbbrgfk.supabase.co

Essa URL identifica o projeto, mas não é a conexão SQL. A senha não foi solicitada nem incluída no pacote.

## 1. Preparar a conexão privada

No Supabase, abra o projeto e clique em **Connect**. Escolha **Session pooler**, formato **URI**, porta **5432**. Copie a conexão para um local privado e substitua o marcador de senha pela senha do banco. Caracteres especiais na senha precisam de codificação de URL. A própria tela do Supabase informa os parâmetros corretos; não deduza a região pelo endereço do projeto.

Essa conexão ficará somente em **DATABASE_URL**, nas variáveis privadas do Render. Não coloque no GitHub, no ZIP público, nos arquivos do Netlify ou no chat. Não usamos a chave pública ou a service_role para essa conexão.

## 2. Enviar o código para o GitHub

Crie um repositório privado e envie o conteúdo desta pasta. **server.py**, **wsgi.py**, **requirements.txt** e **render.yaml** devem ficar na raiz do repositório.

Não envie a instalação local inteira. Não envie **data**, bancos **.db**, **.env**, credenciais ou backups. Esta pasta Cloud foi preparada sem os dados locais.

## 3. Criar o serviço gratuito no Render

No Render, escolha **New → Web Service** e conecte o repositório.

- Runtime: Python.
- Plano: Free.
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn wsgi:application --bind 0.0.0.0:$PORT --workers 1 --threads 1 --timeout 120`
- Health Check Path: `/healthz`

Cadastre as variáveis privadas:

| Variável | Valor |
|---|---|
| DATABASE_URL | URI privada do Session pooler do Supabase |
| ORBIT_V2_PUBLIC_URL | https://prospecstxx.netlify.app |
| ORBIT_V2_BACKEND_URL | URL HTTPS que o Render atribuir ao seu serviço |
| ADMIN_EMAIL | Seu e-mail para o administrador online |
| ADMIN_PASSWORD | Senha inicial com pelo menos 12 caracteres |

O administrador online é separado da instalação local. O primeiro acesso cria o administrador se ele ainda não existir. Depois de confirmar que ele foi criado, remova **ADMIN_PASSWORD** das variáveis do Render. Em reinícios posteriores, o administrador existente é preservado.

O backend usa PostgreSQL no Supabase para todos os dados. Ele recusa inicializar sem DATABASE_URL, evitando usar banco temporário silenciosamente. Há um schema privado para administração e outro por agência. Integrações externas continuam bloqueadas nesta etapa.

## 4. Conectar o Netlify após validar o backend

Depois de o serviço ficar saudável, informe apenas a URL pública do Render para gerar o pacote final do Netlify. Não é uma senha.

O pacote final encaminhará as rotas ao backend, inclusive login e painel. O pacote de tela inicial que já está publicado mantém o botão de login desativado até essa conexão. Não altere a publicação atual antes de validar o servidor.

## 5. Validar antes de convidar clientes

Confirmar conexão real ao PostgreSQL, migrações, login, criação de agência, isolamento de leads entre duas contas, vigência, suspensão, pagamentos, reinício e recuperação de backup. Testes locais não substituem essa validação no ambiente hospedado.

Esta versão não migra automaticamente os dados locais. O banco online começa vazio. A adaptação PostgreSQL foi preparada, mas ainda não foi executada contra este projeto Supabase.

## Limites do plano gratuito

Render pode suspender o serviço por inatividade. Supabase tem cotas gratuitas e pode pausar projetos pouco utilizados. Antes de atender clientes com expectativa de disponibilidade contínua, revisar os limites e planejar backup externo e a passagem para infraestrutura paga.
