/* walksat3.c - portfolio SLS: Novelty+ (mode 3) + WalkSAT noise arg + intensify (2) + cubes
 * build: gcc -O3 -march=native -funroll-loops -o walksat3 walksat3.c
 * usage: ./walksat3 <cnf> <seed> <seconds> <stagnation> <bestfile> [mode] [cubefile] [cs] [ce] [noise_shift] [walk_num] [walk_den]
 *
 * mode: 1 = WalkSAT greedy (min-break) + noise (prob 2^-noise_shift, default shift=4 => 6.25%)
 *       4 = Sparrow (promising set = min-break vars; least-recently-flipped menang kalau di set)
 *       2 = intensify (no noise, flip terarah klausa unsat tersering, scan sering, restart kecil)
 *       3 = Novelty+ (min-break, tie-break variabel paling lama gak di-flip; walk prob walk_num/walk_den)
 *
 * cubefile: tiap baris "k v1..vk b1..bk"; worker proses cube [cs,ce), per-cube budget proporsional.
 * input CNF: baris 1 "n m", lalu satu klausa per baris.
 * output: RESULT UNSAT <u> / ASSIGN <bitstring> / STATS ...
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <math.h>

static int n, m, L;
static int *cstart, *lits, *ostart, *occ_lit, *occ_cl, *sc, *ust, *upos, *freq;
static int *cw;                      /* DLM: bobot klausa (mode 6) */
static int nunsat;
static char *val, *bestval;
static int best_u;
static int MODE = 1;
static int NOISE_SHIFT = 4;
static double BETA = 2.6;              /* probSAT beta (mode 5) */
static double *wtab;                   /* probSAT weight table (1+b)^-beta */
#define TMAX 8192
static int WALK_NUM = 1, WALK_DEN = 2;
static long long *lastf;   /* flip counter terakhir per variabel (Novelty+) */
static long long flipc = 0;

/* cubes */
static int **cube_vars, **cube_bits, *cube_k, n_cubes;

static unsigned long long rs;
static inline unsigned long long xr(void){ rs^=rs<<13; rs^=rs>>7; rs^=rs<<17; return rs; }
static inline int xri(int mod){ return mod>0 ? (int)(xr()%(unsigned long long)mod) : 0; }

static inline int lit_true(int l){ return val[l>0?l:-l] == (l>0?1:0); }

static void push_unsat(int c){ ust[nunsat]=c; upos[c]=nunsat; nunsat++; }
static void pop_unsat(int c){ int p=upos[c]; int last=ust[--nunsat]; ust[p]=last; upos[last]=p; upos[c]=-1; }

static inline void do_flip(int v){
    val[v]^=1;
    for(int i=ostart[v]; i<ostart[v+1]; i++){
        int l=occ_lit[i], c=occ_cl[i];
        if(lit_true(l)){ if(sc[c]++==0) pop_unsat(c); }
        else           { if(--sc[c]==0){ push_unsat(c); cw[c]++; } }
    }
}

static int breakcount(int v){
    int b=0;
    for(int i=ostart[v]; i<ostart[v+1]; i++){
        int l=occ_lit[i], c=occ_cl[i];
        if(sc[c]==1 && lit_true(l)) b++;
    }
    return b;
}

static void rebuild(void){
    nunsat=0;
    for(int c=0;c<m;c++){ upos[c]=-1; sc[c]=0; }
    for(int c=0;c<m;c++){
        int s=0;
        for(int i=cstart[c]; i<cstart[c+1]; i++) if(lit_true(lits[i])) s++;
        sc[c]=s;
        if(s==0) push_unsat(c);
    }
}

static void save_best(void){
    if(nunsat<best_u){ best_u=nunsat; memcpy(bestval+1, val+1, n); }
}

static void rand_init(void){ for(int v=1;v<=n;v++) val[v]=(char)(xr()&1); rebuild(); }

static void noise_restart(int k){
    memcpy(val+1, bestval+1, n);
    for(int i=0;i<k;i++){ int v=1+xri(n); val[v]^=1; }
    rebuild();
}

static void write_best(const char *path){
    if(!path) return;
    FILE *f=fopen(path,"w"); if(!f) return;
    for(int i=1;i<=n;i++) fputc(bestval[i] ? '1' : '0', f);
    fputc('\n',f); fclose(f);
}

static int read_best(const char *path){
    if(!path) return 0;
    FILE *f=fopen(path,"r"); if(!f) return 0;
    char *buf=malloc(n+2);
    size_t r=fread(buf,1,n+1,f);
    fclose(f);
    if((int)r<n){ free(buf); return 0; }
    for(int i=0;i<n;i++) val[i+1] = (buf[i]=='1') ? 1 : 0;
    free(buf);
    rebuild(); save_best();
    return 1;
}

static double now_s(void){
    struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t);
    return t.tv_sec + t.tv_nsec/1e9;
}

static int step_once(int stall_limit, int *since, int *restarts, int *scans){
    if(nunsat<=0) return 0;
    int c;
    if(MODE==2){
        int bc=-1, bf=-1;
        for(int t=0;t<4;t++){ int cc=ust[xri(nunsat)]; if(++freq[cc]>bf){ bf=freq[cc]; bc=cc; } }
        c=bc;
    } else if(MODE==6){
        /* DLM (dynamic local search / clause weighting): sample 8 klausa unsat,
           pilih yang bobotnya terbesar (bobot naik tiap klausa jadi unsat),
           lalu flip variabel min-break. Smoothing bobot tiap 500k flip di main. */
        int bc=ust[xri(nunsat)], bw=-1;
        for(int t=0;t<8;t++){ int cc=ust[xri(nunsat)]; if(cw[cc]>bw){ bw=cw[cc]; bc=cc; } }
        c=bc;
        int bmin=1<<30;
        for(int i=cstart[c];i<cstart[c+1];i++){
            int v=lits[i]>0?lits[i]:-lits[i];
            int b=breakcount(v);
            if(b<bmin) bmin=b;
        }
        int pv[256], cnt=0;
        for(int i=cstart[c];i<cstart[c+1];i++){
            int v=lits[i]>0?lits[i]:-lits[i];
            if(breakcount(v)==bmin && cnt<256) pv[cnt++]=v;
        }
        if(cnt==0) return 0;
        int bv=pv[xri(cnt)];
        do_flip(bv);
        if(nunsat<best_u){ best_u=nunsat; memcpy(bestval+1,val+1,n); *since=0; }
        return 1;
    } else {
        c=ust[xri(nunsat)];
    }
    int bv=-1, bb=1<<30;
    if(MODE==5){
        /* probSAT (Balint & Schoening): pilih variabel dari klausa unsat dengan
           probabilitas proporsional (1+break)^-beta. Spesialis random k-SAT. */
        int len = cstart[c+1]-cstart[c];
        if(len>64) len=64;
        double w[64], sum=0; int vars[64];
        for(int j=0;j<len;j++){
            int v=lits[cstart[c]+j]>0?lits[cstart[c]+j]:-lits[cstart[c]+j];
            vars[j]=v;
            int b=breakcount(v);
            double wj = b<=TMAX ? wtab[b] : pow(1.0+b,-BETA);
            w[j]=wj; sum+=wj;
        }
        double r=(xr()>>11)*(1.0/9007199254740992.0)*sum;
        int pick=len-1;
        for(int j=0;j<len;j++){ r-=w[j]; if(r<=0){ pick=j; break; } }
        bv=vars[pick];
        if(bv<0) return 0;
        do_flip(bv);
        if(nunsat<best_u){ best_u=nunsat; memcpy(bestval+1,val+1,n); *since=0; }
        return 1;
    } else if(MODE==4){
        /* Sparrow: promising set = variabel dgn break minimum; least-recently-flipped
           menang kalau ada di set, kalau tidak random dari set */
        flipc++;
        if(WALK_DEN>0 && (xr()%(unsigned long long)WALK_DEN)<(unsigned long long)WALK_NUM){
            int k=xri(cstart[c+1]-cstart[c]);
            int l=lits[cstart[c]+k];
            bv=l>0?l:-l;
        } else {
            int bmin=1<<30;
            for(int i=cstart[c];i<cstart[c+1];i++){
                int v=lits[i]>0?lits[i]:-lits[i];
                int b=breakcount(v);
                if(b<bmin) bmin=b;
            }
            int lr=-1; long long lrt=9223372036854775807LL; int cnt=0; int pv[256];
            for(int i=cstart[c];i<cstart[c+1];i++){
                int v=lits[i]>0?lits[i]:-lits[i];
                if(lastf[v]<lrt){ lrt=lastf[v]; lr=v; }
                if(breakcount(v)==bmin && cnt<256) pv[cnt++]=v;
            }
            if(cnt==0){ int k=xri(cstart[c+1]-cstart[c]); int l=lits[cstart[c]+k]; bv=l>0?l:-l; }
            else {
                int inP=0;
                for(int i=0;i<cnt;i++) if(pv[i]==lr) inP=1;
                bv = inP ? lr : pv[xri(cnt)];
            }
        }
        if(bv<0) return 0;
        do_flip(bv); lastf[bv]=flipc;
    } else if(MODE==3){
        /* Novelty+ */
        flipc++;
        if(WALK_DEN>0 && (xr()%(unsigned long long)WALK_DEN)<(unsigned long long)WALK_NUM){
            int k=xri(cstart[c+1]-cstart[c]);
            int l=lits[cstart[c]+k];
            bv=l>0?l:-l;
        } else {
            int b1=1<<30, v1=-1, v2=-1, b2=1<<30;
            for(int i=cstart[c];i<cstart[c+1];i++){
                int v=lits[i]>0?lits[i]:-lits[i];
                int b=breakcount(v);
                if(b<b1){ b2=b1; v2=v1; b1=b; v1=v; }
                else if(b<b2){ b2=b; v2=v; }
            }
            if(v2<0){ bv=v1; }
            else {
                int lr = (lastf[v1] <= lastf[v2]) ? v1 : v2;   /* paling lama gak di-flip */
                int ot = (lr==v1) ? v2 : v1;
                bv = ((xr()%100) < 98) ? lr : ot;
            }
        }
        if(bv<0) return 0;
        do_flip(bv); lastf[bv]=flipc;
    } else {
        if(MODE!=2 && (xr() & ((1<<NOISE_SHIFT)-1))==0){
            int k=xri(cstart[c+1]-cstart[c]);
            int l=lits[cstart[c]+k];
            bv=l>0?l:-l;
        } else {
            for(int i=cstart[c];i<cstart[c+1];i++){
                int v=lits[i]>0?lits[i]:-lits[i];
                int b=breakcount(v);
                if(b<bb || (b==bb && (xr()&1))){ bb=b; bv=v; }
            }
        }
        if(bv<0) return 0;
        do_flip(bv);
    }
    if(nunsat<best_u){
        best_u=nunsat; memcpy(bestval+1,val+1,n); *since=0;
    } else if(++(*since)>stall_limit){
        (*scans)++;
        int gv=-1, gd=-(1<<30);
        for(int v=1;v<=n;v++){
            int b=0,g=0;
            for(int i=ostart[v];i<ostart[v+1];i++){
                int l=occ_lit[i], cc=occ_cl[i];
                if(lit_true(l)){ if(sc[cc]==1) b++; }
                else           { if(sc[cc]==0) g++; }
            }
            if(g-b>gd){ gd=g-b; gv=v; }
        }
        if(gv>0 && gd>0){ do_flip(gv); }
        else { (*restarts)++; noise_restart(MODE==2 ? 1+xri(4) : (best_u<=8 ? 1+xri(8) : 1+xri(n/8+1))); }
        *since=0;
    }
    return 1;
}

static void load_cubes(const char *path){
    FILE *f=fopen(path,"r"); if(!f) return;
    int cap=16; n_cubes=0;
    cube_vars=malloc(cap*sizeof(int*)); cube_bits=malloc(cap*sizeof(int*)); cube_k=malloc(cap*sizeof(int));
    int k;
    while(fscanf(f,"%d",&k)==1){
        if(k<=0) continue;
        if(n_cubes==cap){ cap*=2;
            cube_vars=realloc(cube_vars,cap*sizeof(int*));
            cube_bits=realloc(cube_bits,cap*sizeof(int*));
            cube_k=realloc(cube_k,cap*sizeof(int)); }
        cube_vars[n_cubes]=malloc(k*sizeof(int));
        cube_bits[n_cubes]=malloc(k*sizeof(int));
        for(int i=0;i<k;i++) if(fscanf(f,"%d",&cube_vars[n_cubes][i])!=1) return;
        for(int i=0;i<k;i++) if(fscanf(f,"%d",&cube_bits[n_cubes][i])!=1) return;
        cube_k[n_cubes]=k; n_cubes++;
    }
    fclose(f);
}

int main(int argc, char **argv){
    if(argc<5){ fprintf(stderr,"usage: %s <cnf> <seed> <seconds> <stagnation> <bestfile> [mode] [cubefile] [cs] [ce] [noise_shift] [walk_num] [walk_den]\n",argv[0]); return 2; }

    FILE *f=fopen(argv[1],"r");
    if(!f){ perror("open cnf"); return 2; }
    fseek(f,0,SEEK_END); long sz=ftell(f); fseek(f,0,SEEK_SET);
    char *buf=malloc(sz+1);
    if(fread(buf,1,sz,f)!=(size_t)sz){ perror("read"); return 2; }
    buf[sz]=0; fclose(f);

    char *p=buf;
    n=(int)strtol(p,&p,10);
    m=(int)strtol(p,&p,10);
    if(n<=0||m<=0){ fprintf(stderr,"bad header\n"); return 2; }

    cstart=malloc((m+1)*sizeof(int));
    int cap=1024, Lc=0;
    int *li=malloc(cap*sizeof(int));
    int ci=0; cstart[0]=0;
    while(*p && ci<m){
        while(*p && *p!='\n') p++;
        if(*p=='\n') p++;
        while(*p==' '||*p=='\t'||*p=='\r') p++;
        if(*p==0) break;
        cstart[ci]=Lc;
        while(*p && *p!='\n'){
            char *e; long v=strtol(p,&e,10);
            if(e==p){ p++; continue; }
            if(Lc==cap){ cap*=2; li=realloc(li,cap*sizeof(int)); }
            li[Lc++]=(int)v; p=e;
        }
        ci++;
    }
    m=ci; cstart[m]=Lc; L=Lc; lits=li;

    ostart=calloc(n+2,sizeof(int));
    int *cnt=calloc(n+2,sizeof(int));
    for(int i=0;i<L;i++){ int v=lits[i]>0?lits[i]:-lits[i]; cnt[v]++; }
    for(int v=1;v<=n;v++) ostart[v+1]=ostart[v]+cnt[v];
    occ_lit=malloc((L?L:1)*sizeof(int));
    occ_cl =malloc((L?L:1)*sizeof(int));
    int *fill=malloc((n+2)*sizeof(int));
    for(int v=1;v<=n;v++) fill[v]=ostart[v];
    for(int c=0;c<m;c++)
        for(int i=cstart[c];i<cstart[c+1];i++){
            int v=lits[i]>0?lits[i]:-lits[i];
            int k=fill[v]++;
            occ_lit[k]=lits[i]; occ_cl[k]=c;
        }

    sc=malloc(m*sizeof(int));
    ust=malloc(m*sizeof(int));
    upos=malloc(m*sizeof(int));
    freq=calloc(m,sizeof(int));
    cw=calloc(m,sizeof(int));
    val=calloc(n+1,1);
    bestval=calloc(n+1,1);
    lastf=calloc(n+1,sizeof(long long));
    best_u=n+1;

    rs=strtoull(argv[2],NULL,10); if(!rs) rs=0x9e3779b97f4a7c15ULL;
    double tlimit=atof(argv[3]);
    int stall_limit=atoi(argv[4]); if(stall_limit<=0) stall_limit=50000;
    const char *bestfile=argv[5];
    if(argc>6) MODE=atoi(argv[6]);
    int cube_start=0, cube_end=0;
    if(argc>9){ load_cubes(argv[7]); cube_start=atoi(argv[8]); cube_end=atoi(argv[9]); }
    if(argc>10) NOISE_SHIFT=atoi(argv[10]);
    if(NOISE_SHIFT<0) NOISE_SHIFT=0; if(NOISE_SHIFT>30) NOISE_SHIFT=30;
    if(argc>11) WALK_NUM=atoi(argv[11]);
    if(argc>12) WALK_DEN=atoi(argv[12]);
    if(WALK_DEN<1) WALK_DEN=1;
    if(argc>13) BETA=atof(argv[13]);
    if(BETA<=0) BETA=2.6;
    if(MODE==5){
        wtab=malloc(sizeof(double)*(TMAX+1));
        for(int b=0;b<=TMAX;b++) wtab[b]=pow(1.0+b,-BETA);
    }

    double t0=now_s();
    rand_init(); save_best();
    if(read_best(bestfile)) { }

    long long flips=0; int restarts=0, scans=0, since=0;
    long long last_write=-1;

    if(n_cubes>0 && cube_end>cube_start){
        for(int ci2=cube_start; ci2<cube_end && ci2<n_cubes; ci2++){
            double lg=tlimit-(now_s()-t0);
            if(lg<=0.5) break;
            double per=lg/(double)(cube_end-ci2);
            if(per<1.0) per=1.0;
            double cd=now_s()+per;
            read_best(bestfile);
            for(int i=0;i<cube_k[ci2];i++) val[cube_vars[ci2][i]]=(char)cube_bits[ci2][i];
            rebuild(); save_best();
            long long cf=0;
            while(nunsat>0){
                if((cf & 4095)==0){ double nn=now_s(); if(nn-t0>=tlimit||nn>=cd) break; }
                if(!step_once(stall_limit,&since,&restarts,&scans)) break;
                cf++; flips++;
                if(nunsat<best_u && bestfile && (flips-last_write>50000)){ write_best(bestfile); last_write=flips; }
            }
            if(bestfile) write_best(bestfile);
            if(nunsat==0) break;
        }
    } else {
        while(nunsat>0){
            if((flips & 8191)==0 && now_s()-t0>=tlimit) break;
            if(!step_once(stall_limit,&since,&restarts,&scans)) break;
            flips++;
            if(MODE==6 && (flips & 524287)==0){ for(int c2=0;c2<m;c2++) cw[c2]=cw[c2]/2+1; }
            if(nunsat<best_u && bestfile && (flips-last_write>50000)){ write_best(bestfile); last_write=flips; }
        }
    }

    double el=now_s()-t0;
    if(bestfile) write_best(bestfile);
    printf("RESULT UNSAT %d\n", best_u);
    printf("ASSIGN ");
    for(int i=1;i<=n;i++) putchar(bestval[i] ? '1' : '0');
    printf("\n");
    printf("STATS flips=%lld restarts=%d scans=%d secs=%.2f rate=%.0f best=%d mode=%d cubes=%d\n",
           flips, restarts, scans, el, flips/(el>0?el:1.0), best_u, MODE, n_cubes);
    return best_u==0 ? 0 : 1;
}
