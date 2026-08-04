import { useMemo, useRef } from 'react';
import { Canvas, useFrame } from '@react-three/fiber';
import { amplitudeVoix, respirationSynthetique } from './voixAmplitude';
import * as THREE from 'three';
import { particleVertex, particleFragment, lineVertex, lineFragment } from './shaders/orbShaders';
import { STATES, type ImmersiveState } from './immersiveStates';

const PARTICLES = 1000;

interface Sphere {
  positions: Float32Array;
  seeds: Float32Array;
}
function makeFibonacciSphere(count: number): Sphere {
  const positions = new Float32Array(count * 3);
  const seeds = new Float32Array(count);
  const GA = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < count; i++) {
    const y = 1 - (i / (count - 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    const theta = GA * i;
    positions[i * 3 + 0] = Math.cos(theta) * r;
    positions[i * 3 + 1] = y;
    positions[i * 3 + 2] = Math.sin(theta) * r;
    seeds[i] = Math.random();
  }
  return { positions, seeds };
}

function buildKNN(positions: Float32Array, count: number, k = 3): [number, number][] {
  const pairs: [number, number][] = [];
  const seen = new Set<string>();
  for (let i = 0; i < count; i++) {
    const pi = i * 3;
    const dists: [number, number][] = [];
    for (let j = 0; j < count; j++) {
      if (j === i) continue;
      const pj = j * 3;
      const dx = positions[pi] - positions[pj];
      const dy = positions[pi + 1] - positions[pj + 1];
      const dz = positions[pi + 2] - positions[pj + 2];
      dists.push([dx * dx + dy * dy + dz * dz, j]);
    }
    dists.sort((a, b) => a[0] - b[0]);
    for (let m = 0; m < k; m++) {
      const j = dists[m][1];
      const key = i < j ? `${i},${j}` : `${j},${i}`;
      if (!seen.has(key)) {
        seen.add(key);
        pairs.push([i, j]);
      }
    }
  }
  return pairs;
}

interface OrbGroupProps {
  state: ImmersiveState;
  groupScale: number;
}

function OrbGroup({ state, groupScale }: OrbGroupProps) {
  const groupRef = useRef<THREE.Group>(null!);
  const orbRef = useRef<THREE.Points>(null!);
  const linesRef = useRef<THREE.LineSegments>(null!);
  const innerWireRef = useRef<THREE.Mesh>(null!);
  const outerWireRef = useRef<THREE.Mesh>(null!);

  const { orbGeo, lineGeo } = useMemo(() => {
    const sph = makeFibonacciSphere(PARTICLES);
    const orbGeo = new THREE.BufferGeometry();
    orbGeo.setAttribute('position', new THREE.BufferAttribute(sph.positions, 3));
    orbGeo.setAttribute('aSeed', new THREE.BufferAttribute(sph.seeds, 1));

    const pairs = buildKNN(sph.positions, PARTICLES, 3);
    const linePositions = new Float32Array(pairs.length * 6);
    const lineSeeds = new Float32Array(pairs.length * 2);
    for (let p = 0; p < pairs.length; p++) {
      const [i, j] = pairs[p];
      linePositions[p * 6 + 0] = sph.positions[i * 3 + 0];
      linePositions[p * 6 + 1] = sph.positions[i * 3 + 1];
      linePositions[p * 6 + 2] = sph.positions[i * 3 + 2];
      linePositions[p * 6 + 3] = sph.positions[j * 3 + 0];
      linePositions[p * 6 + 4] = sph.positions[j * 3 + 1];
      linePositions[p * 6 + 5] = sph.positions[j * 3 + 2];
      lineSeeds[p * 2 + 0] = sph.seeds[i];
      lineSeeds[p * 2 + 1] = sph.seeds[j];
    }
    const lineGeo = new THREE.BufferGeometry();
    lineGeo.setAttribute('position', new THREE.BufferAttribute(linePositions, 3));
    lineGeo.setAttribute('aSeed', new THREE.BufferAttribute(lineSeeds, 1));
    return { orbGeo, lineGeo };
  }, []);

  const uniforms = useMemo(() => {
    const init = STATES.idle;
    return {
      time: { value: 0 },
      breathing: { value: init.breathing },
      noiseScale: { value: 1.4 },
      noiseAmplitude: { value: init.noiseAmp },
      pointSize: { value: init.pointSize },
      radius: { value: init.radius },
      opacity: { value: 0.30 },
      colorA: { value: new THREE.Color(init.colorA) },
      colorB: { value: new THREE.Color(init.colorB) },
    };
  }, []);

  const lineUniforms = useMemo(() => {
    const init = STATES.idle;
    return {
      time: uniforms.time,
      breathing: uniforms.breathing,
      noiseScale: uniforms.noiseScale,
      noiseAmplitude: uniforms.noiseAmplitude,
      radius: uniforms.radius,
      color: { value: new THREE.Color(init.colorA) },
      opacity: { value: init.linkOp },
    };
  }, [uniforms]);

  // Amplitude de voix lissée entre deux images — `useRef` et non `useState` :
  // elle change 60 fois par seconde et ne doit JAMAIS déclencher de rendu React.
  const voixLissee = useRef(0);
  const targetColA = useRef(new THREE.Color(STATES.idle.colorA));
  const targetColB = useRef(new THREE.Color(STATES.idle.colorB));

  useFrame((_, dt) => {
    const target = STATES[state];
    const lerp = Math.min(2.0 * dt, 0.2);

    uniforms.noiseAmplitude.value += (target.noiseAmp - uniforms.noiseAmplitude.value) * lerp;
    uniforms.breathing.value      += (target.breathing - uniforms.breathing.value) * lerp;
    uniforms.radius.value         += (target.radius - uniforms.radius.value) * lerp;
    uniforms.pointSize.value      += (target.pointSize - uniforms.pointSize.value) * lerp;
    lineUniforms.opacity.value    += (target.linkOp - lineUniforms.opacity.value) * lerp;

    targetColA.current.set(target.colorA);
    targetColB.current.set(target.colorB);
    uniforms.colorA.value.lerp(targetColA.current, lerp);
    uniforms.colorB.value.lerp(targetColB.current, lerp);
    lineUniforms.color.value.lerp(targetColA.current, lerp);

    if (innerWireRef.current && outerWireRef.current) {
      const innerMat = innerWireRef.current.material as THREE.MeshBasicMaterial;
      const outerMat = outerWireRef.current.material as THREE.MeshBasicMaterial;
      outerMat.opacity += (target.wireOp - outerMat.opacity) * lerp;
      innerMat.opacity += (target.wireOp * 2.5 - innerMat.opacity) * lerp;
      outerMat.color.lerp(targetColA.current, lerp);
      innerMat.color.lerp(targetColB.current, lerp);
    }

    // ══ L'orbe PARLE ══════════════════════════════════════════════════════════════
    // ⚠ Sans ce bloc, `speaking` n'était qu'un ÉTAT : l'orbe changeait d'apparence au
    //   début de la réponse puis restait figée jusqu'à la fin. Elle est maintenant
    //   pilotée par l'amplitude RÉELLE de la voix (analyseur inséré dans la chaîne
    //   audio), donc elle articule au lieu de vibrer.
    // ⚠ Repli synthétique quand aucun audio n'est disponible — voix coupée, TTS en
    //   échec, lecture bloquée faute de geste utilisateur. Sans lui l'orbe resterait
    //   parfaitement immobile pendant qu'un texte défile : pire que l'animation
    //   constante d'avant, parce qu'elle aurait l'air plantée.
    const parle = state === 'speaking';
    const brut = parle ? amplitudeVoix() : 0;
    const voix = parle
      ? (brut > 0.02 ? brut : respirationSynthetique(uniforms.time.value))
      : 0;
    // Lissage supplémentaire côté rendu : l'amplitude audio saute d'une image à
    // l'autre, et une sphère qui suit chaque pic donne un tremblement, pas une voix.
    voixLissee.current += (voix - voixLissee.current) * Math.min(8 * dt, 0.35);
    const v = voixLissee.current;

    if (parle || v > 0.001) {
      // Trois effets, parce qu'un seul se lit comme un clignotement :
      //  · le RAYON gonfle       → la sphère « prend de l'air » sur les voyelles ;
      //  · le BRUIT s'agite      → la surface se déforme, elle n'est pas rigide ;
      //  · les POINTS grossissent → l'énergie se voit jusque dans la matière.
      //
      // ⚠ ON POSE LA VALEUR, ON NE L'INCRÉMENTE PAS — corrigé après l'audit du
      //   2026-08-04, qui a démontré le défaut par simulation de la boucle réelle.
      //   `uniforms` est un objet `useMemo([])`, donc PERSISTANT entre les images : un
      //   `+=` s'ajoutait à la valeur déjà interpolée quelques lignes plus haut, image
      //   après image. Les deux forces finissaient par s'équilibrer, mais à un point
      //   dépendant de la fréquence d'affichage — l'orbe respirait donc plus fort sur un
      //   écran 144 Hz que sur un 60 Hz, et dérivait pendant les longues réponses.
      //   `target.*` est la cible de l'état courant : la modulation s'y applique
      //   proprement, sans mémoire d'une image à l'autre.
      uniforms.radius.value = target.radius + v * 0.16;
      uniforms.noiseAmplitude.value = target.noiseAmp + v * 0.35;
      uniforms.pointSize.value = target.pointSize + v * 1.6;
      if (groupRef.current) {
        // ⚠ Amplitude volontairement FAIBLE (4 %) : au-delà, la sphère « saute » et
        //   l'effet devient comique. C'est la somme des trois effets qui donne
        //   l'impression de parole, pas l'ampleur de l'un d'eux.
        //   ⚠ `setScalar` et non `multiplyScalar` : ce dernier multipliait l'échelle
        //     DÉJÀ modulée de l'image précédente — la sphère enflait sans fin pendant
        //     une longue réponse.
        groupRef.current.scale.setScalar(groupScale * (1 + v * 0.04));
      }
    }

    uniforms.time.value += dt * target.speed * 2.4;

    if (groupRef.current) {
      groupRef.current.rotation.y += dt * 0.08;
      // smooth group scale update (avoids pop when viewport resize)
      const currentScale = groupRef.current.scale.x;
      const newScale = currentScale + (groupScale - currentScale) * lerp;
      groupRef.current.scale.setScalar(newScale);
    }
    if (orbRef.current) orbRef.current.rotation.y += dt * 0.10;
    if (linesRef.current && orbRef.current) linesRef.current.rotation.copy(orbRef.current.rotation);
    if (outerWireRef.current) {
      outerWireRef.current.rotation.y -= dt * 0.06;
      outerWireRef.current.rotation.x = Math.sin(uniforms.time.value * 0.2) * 0.1;
    }
    if (innerWireRef.current) {
      innerWireRef.current.rotation.y += dt * 0.12;
      innerWireRef.current.rotation.x -= dt * 0.08;
    }
  });

  return (
    <group ref={groupRef} position={[0, 0, -1.0]} scale={groupScale}>
      <points ref={orbRef} geometry={orbGeo}>
        <shaderMaterial
          attach="material"
          vertexShader={particleVertex}
          fragmentShader={particleFragment}
          uniforms={uniforms}
          transparent
          depthWrite={false}
          blending={THREE.AdditiveBlending}
        />
      </points>

      <lineSegments ref={linesRef} geometry={lineGeo}>
        <shaderMaterial
          attach="material"
          vertexShader={lineVertex}
          fragmentShader={lineFragment}
          uniforms={lineUniforms}
          transparent
          depthWrite={false}
          blending={THREE.AdditiveBlending}
        />
      </lineSegments>

      <mesh ref={innerWireRef}>
        <icosahedronGeometry args={[0.42, 1]} />
        <meshBasicMaterial color="#7fb9e8" wireframe transparent opacity={0.15} depthWrite={false} />
      </mesh>

      <mesh ref={outerWireRef}>
        <icosahedronGeometry args={[1.55, 1]} />
        <meshBasicMaterial color="#51a4de" wireframe transparent opacity={0.08} depthWrite={false} />
      </mesh>
    </group>
  );
}

interface OrbAuraProps {
  state: ImmersiveState;
  groupScale?: number;
  /**
   * Cadre du canvas. Par défaut plein écran (`inset: 0`) — la vue « présence » d'origine.
   *
   * ⚠ EXISTE POUR METTRE L'ORBE DANS UN COIN (demande de l'admin, 2026-08-04 : « le
   *   terminal de chat avec l'historique et l'affichage d'Ava un peu plus petit ou dans
   *   un coin »). Réduire seulement `groupScale` ne suffisait pas : le canvas reste
   *   plein écran, donc l'orbe reste CENTRÉE — juste plus petite, et toujours derrière
   *   le terminal. Il faut déplacer le cadre, pas la sphère.
   */
  cadre?: React.CSSProperties;
}

export function OrbAura({ state, groupScale = 1.3, cadre }: OrbAuraProps) {
  return (
    <Canvas
      style={{ position: 'fixed', inset: 0, pointerEvents: 'none', ...cadre }}
      camera={{ position: [0, 0, 7.5], fov: 45 }}
      dpr={[1, 2]}
      gl={{ antialias: true, alpha: true }}
    >
      <OrbGroup state={state} groupScale={groupScale} />
    </Canvas>
  );
}
