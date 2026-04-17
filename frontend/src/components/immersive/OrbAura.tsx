import { useMemo, useRef } from 'react';
import { Canvas, useFrame } from '@react-three/fiber';
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
}

export function OrbAura({ state, groupScale = 1.3 }: OrbAuraProps) {
  return (
    <Canvas
      style={{ position: 'fixed', inset: 0, pointerEvents: 'none' }}
      camera={{ position: [0, 0, 7.5], fov: 45 }}
      dpr={[1, 2]}
      gl={{ antialias: true, alpha: true }}
    >
      <OrbGroup state={state} groupScale={groupScale} />
    </Canvas>
  );
}
